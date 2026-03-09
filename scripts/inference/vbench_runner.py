"""
vbench_runner.py  --  Python replacement for vbench_runner.ps1
Calls sample_5b.py once per prompt with NUM_SAMPLES caption lines,
so the model loads once per prompt instead of once per sample.
"""
import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ALLOWED_TYPES = ['indoor', 'scenery']
NUM_SAMPLES   = 5
NUM_FRAMES    = 161


# ---------- helpers ----------

def _safe(s, maxlen=80):
    return re.sub(r'[<>:"/\\|?*]', '_', s)[:maxlen]

def _poll_vram(stop_event, readings):
    while not stop_event.is_set():
        try:
            out = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                stderr=subprocess.DEVNULL, text=True
            ).strip().splitlines()[0].strip()
            if out.isdigit():
                readings.append(int(out))
        except Exception:
            pass
        time.sleep(5)

def _ram_gb():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.used / (1024 ** 3), 2)
    except Exception:
        return ''

def _vram_peak_gb(readings):
    if not readings:
        return ''
    return round(max(readings) / 1024.0, 2)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-seed',   required=True, type=int)
    ap.add_argument('--vbench-json', required=True)
    ap.add_argument('--vbench-crop', required=True)
    ap.add_argument('--work-dir',    required=True)
    args = ap.parse_args()

    work_dir   = Path(args.work_dir)
    out_base   = work_dir / 'outputs' / 'vbench' / 'videos'
    stats_path = work_dir / 'outputs' / 'vbench' / 'stats.csv'

    if not Path(args.vbench_json).exists():
        sys.exit(f'[vbench] ERROR: JSON not found: {args.vbench_json}')
    if not Path(args.vbench_crop).exists():
        sys.exit(f'[vbench] ERROR: crop dir not found: {args.vbench_crop}')

    with open(args.vbench_json, encoding='utf-8') as f:
        entries = json.load(f)

    seen, prompts = set(), []
    for e in entries:
        fn = e.get('file_name', '')
        if fn in seen:
            continue
        if e.get('type') not in ALLOWED_TYPES:
            continue
        seen.add(fn)
        cap = e.get('caption') or Path(fn).stem
        prompts.append({'file': fn, 'caption': cap, 'type': e['type']})

    total = len(prompts) * NUM_SAMPLES
    print(f'\n=== VBench prompt list ({len(prompts)} prompts, types: {", ".join(ALLOWED_TYPES)}) ===')
    for i, p in enumerate(prompts):
        img_ok = '  ' if (Path(args.vbench_crop) / p['file']).exists() else 'MISSING'
        print(f'  [{i+1:2}/{len(prompts)}] ({p["type"]:<8}) {img_ok} {p["caption"]}')
    print(f'=== {len(prompts)} prompts x {NUM_SAMPLES} samples = {total} runs ===\n')

    out_base.mkdir(parents=True, exist_ok=True)
    stats_path.parent.mkdir(parents=True, exist_ok=True)

    stats_is_new = not stats_path.exists()
    stats_f = open(stats_path, 'a', newline='', encoding='utf-8')
    writer  = csv.writer(stats_f)
    if stats_is_new:
        writer.writerow(['task_idx', 'prompt', 'type', 'sample_idx', 'seed',
                         'duration_s', 'gen_fps', 'vram_gb', 'ram_gb', 'out_path', 'status'])
        stats_f.flush()

    done = generated = errors = skipped = 0
    t_start = time.time()

    for ti, p in enumerate(prompts):
        img_src = Path(args.vbench_crop) / p['file']
        if not img_src.exists():
            print(f'[vbench] skip prompt {ti+1}: image not found - {img_src}')
            done += NUM_SAMPLES
            continue

        safe_cap = _safe(p['caption'])

        # check how many samples already completed (renamed with _s{i}_seed suffix)
        already = sorted(out_base.glob(f'*{safe_cap}*_s[0-9]*_seed*.mp4'))
        n_already = len(already)

        # also check for un-renamed files left by an interrupted run
        orphans = sorted([f for f in out_base.glob(f'*{safe_cap}*.mp4')
                          if '_seed' not in f.name])

        n_needed = NUM_SAMPLES - n_already - len(orphans)

        if n_needed <= 0 and not orphans:
            print(f'[vbench] skip prompt {ti+1}: all {NUM_SAMPLES} samples already done')
            for i, f in enumerate(already):
                writer.writerow([ti, p['caption'], p['type'], i, '', '', '', '', '', str(f), 'skipped'])
            stats_f.flush()
            skipped += NUM_SAMPLES
            done    += NUM_SAMPLES
            continue

        if n_already > 0 or orphans:
            print(f'[vbench] prompt {ti+1}: {n_already} renamed + {len(orphans)} unfinished, resuming {max(0,n_needed)} new')

        # seed for this prompt (reproducible)
        rng  = random.Random(args.base_seed ^ hash(p['caption']))
        seed = rng.randint(0, 2**31 - 1)

        # recover any orphan files left by a previous interrupted run
        for i, src in enumerate(orphans):
            si_abs = n_already + i
            dst = src.with_stem(f'{src.stem}_s{si_abs}_seed{seed}')
            print(f'  [recover] {src.name} -> {dst.name}')
            src.rename(dst)
            writer.writerow([ti, p['caption'], p['type'], si_abs, seed,
                              '', '', '', '', str(dst), 'recovered'])
            stats_f.flush()
            already.append(dst)
        n_already = len(already)
        n_needed  = max(0, NUM_SAMPLES - n_already)

        if n_needed <= 0:
            print(f'[vbench] skip prompt {ti+1}: all {NUM_SAMPLES} samples recovered/done')
            skipped += NUM_SAMPLES
            done    += NUM_SAMPLES
            continue

        pct = round(100 * done / total) if total else 0
        eta = ''
        if done > 0:
            elapsed = time.time() - t_start
            rem = int(elapsed / done * (total - done))
            eta = f' ETA {rem//3600}:{(rem%3600)//60:02d}:{rem%60:02d}'
        short_cap = p['caption'][:60]
        print(f'[vbench] [{done+1}/{total} {pct}%{eta}]  '
              f'prompt {ti+1}/{len(prompts)} ({p["type"]})  '
              f'{n_needed} samples  seed {seed} : {short_cap}')

        # prepare temp dir with exactly this image
        tmp_dir = work_dir / '_vbench_tmp'
        tmp_dir.mkdir(exist_ok=True)
        for f in tmp_dir.iterdir():
            f.unlink(missing_ok=True)
        shutil.copy(img_src, tmp_dir)

        # write n_needed identical caption lines so sample_5b generates n_needed videos
        cap_file = work_dir / '_vbench_caption.txt'
        cap_file.write_text('\n'.join([p['caption']] * n_needed), encoding='utf-8')

        # start VRAM polling
        vram_readings = []
        stop_evt  = threading.Event()
        vram_thread = threading.Thread(target=_poll_vram, args=(stop_evt, vram_readings), daemon=True)
        vram_thread.start()

        t0     = time.time()
        status = 'error'
        new_mp4s = []
        try:
            # snapshot existing files so we can find new ones after subprocess
            pre_existing = set(out_base.glob('*.mp4'))

            env = {**os.environ,
                   'TOKENIZERS_PARALLELISM': 'false',
                   'TF_ENABLE_ONEDNN_OPTS':  '0',
                   'LOCAL_RANK': '0', 'RANK': '0', 'WORLD_SIZE': '1',
                   'MASTER_ADDR': '127.0.0.1', 'MASTER_PORT': '29500'}
            subprocess.run([
                sys.executable, 'fastvideo/sample/sample_5b.py',
                '--seed', str(seed),
                '--gradient_checkpointing',
                '--train_batch_size=1',
                '--max_sample_steps=1',
                '--mixed_precision=bf16',
                '--allow_tf32',
                '--t5_cpu',
                f'--video_output_dir={out_base}',
                f'--jpg_dir={tmp_dir}',
                f'--caption_path={cap_file}',
                '--test_data_dir=./val',
                '--num_euler_timesteps', '5',
                '--rand_num_img', '0.6',
                '--internvl_path', './InternVL3-2B-Instruct',
                '--height', '384',
                '--width', '512',
                '--num_frames', str(NUM_FRAMES),
                '--fps', '24',
            ], cwd=str(work_dir), env=env)

            dur = round(time.time() - t0, 2)
            fps = round(NUM_FRAMES * n_needed / dur, 2)

            # collect new output files via set-difference (avoids mtime issues)
            all_now  = set(out_base.glob('*.mp4'))
            raw_mp4s = sorted(
                [f for f in (all_now - pre_existing)
                 if safe_cap in f.name and '_seed' not in f.name],
                key=lambda f: f.name   # sort by name → _0, _1, _2 ... order
            )
            print(f'  [detect] {len(raw_mp4s)} new mp4s found (pre={len(pre_existing)}, now={len(all_now)})')

            for i, src in enumerate(raw_mp4s):
                si_abs = n_already + i
                dst = src.with_stem(f'{src.stem}_s{si_abs}_seed{seed}')
                src.rename(dst)
                new_mp4s.append(dst)

            if len(new_mp4s) == n_needed:
                status = 'ok'
            else:
                print(f'  WARNING: expected {n_needed} mp4s, got {len(new_mp4s)}')

        except Exception as exc:
            print(f'  EXCEPTION: {exc}', file=sys.stderr)
        finally:
            stop_evt.set()
            vram_thread.join(timeout=10)
            vram = _vram_peak_gb(vram_readings)
            ram  = _ram_gb()

        if status == 'ok':
            print(f'  done in {dur}s  ({fps} gen-fps)  VRAM {vram}GB  RAM {ram}GB')
        for i, mp4 in enumerate(new_mp4s):
            si_abs   = n_already + i
            row_stat = 'ok' if status == 'ok' else 'partial'
            writer.writerow([ti, p['caption'], p['type'], si_abs, seed,
                              dur if i == 0 else '', fps if i == 0 else '',
                              vram if i == 0 else '', ram if i == 0 else '',
                              str(mp4), row_stat])
            stats_f.flush()
        if status == 'ok':
            generated += len(new_mp4s)
            done      += n_needed
        else:
            writer.writerow([ti, p['caption'], p['type'], '', seed,
                              '', '', '', '', '', 'error'])
            stats_f.flush()
            stats_f.close()
            sys.exit(f'[vbench] FATAL error on prompt {ti+1} "{short_cap}" - stopping')

        shutil.rmtree(tmp_dir, ignore_errors=True)
        cap_file.unlink(missing_ok=True)

    stats_f.close()
    elapsed_m = round((time.time() - t_start) / 60, 1)
    print(f'\n[vbench] done - generated={generated}  skipped={skipped}  errors={errors}  elapsed={elapsed_m}m')
    print(f'[vbench] stats -> {stats_path}')


if __name__ == '__main__':
    main()
