"""
vbench_runner.py  --  Python replacement for vbench_runner.ps1
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
    ap.add_argument('--run-ts',      required=True)
    ap.add_argument('--base-seed',   required=True, type=int)
    ap.add_argument('--vbench-json', required=True)
    ap.add_argument('--vbench-crop', required=True)
    ap.add_argument('--work-dir',    required=True)
    args = ap.parse_args()

    work_dir   = Path(args.work_dir)
    out_base   = work_dir / 'outputs' / args.run_ts / 'vbench' / 'videos'
    stats_path = work_dir / 'outputs' / args.run_ts / 'vbench' / 'stats.csv'

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
            print(f'[vbench] skip {ti}: image not found - {img_src}')
            continue

        tmp_dir  = work_dir / '_vbench_tmp'
        tmp_dir.mkdir(exist_ok=True)
        shutil.copy(img_src, tmp_dir)

        cap_file = work_dir / '_vbench_caption.txt'
        cap_file.write_text(p['caption'], encoding='utf-8')

        out_sub = out_base / _safe(p['caption'])
        out_sub.mkdir(parents=True, exist_ok=True)

        existing_mp4s = sorted(out_sub.glob('*.mp4'))

        for si in range(NUM_SAMPLES):
            seed = args.base_seed + si

            if si < len(existing_mp4s):
                skipped += 1; done += 1
                writer.writerow([ti, p['caption'], p['type'], si, seed,
                                  '', '', '', '', str(existing_mp4s[si]), 'skipped'])
                stats_f.flush()
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
                  f'sample {si+1}/{NUM_SAMPLES} : {short_cap}')

            # start VRAM polling thread
            vram_readings = []
            stop_evt = threading.Event()
            vram_thread = threading.Thread(target=_poll_vram, args=(stop_evt, vram_readings), daemon=True)
            vram_thread.start()

            t0 = datetime.now(timezone.utc)
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
                f'--video_output_dir={out_sub}',
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

            stop_evt.set()
            vram_thread.join(timeout=10)

            dur  = (datetime.now(timezone.utc) - t0).total_seconds()
            fps  = round(NUM_FRAMES / dur, 2)
            vram = _vram_peak_gb(vram_readings)
            ram  = _ram_gb()

            # detect actual output mp4
            new_mp4s = [f for f in out_sub.glob('*.mp4')
                        if f.stat().st_mtime >= t0.timestamp()]
            new_mp4s.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            if new_mp4s:
                out_path = str(new_mp4s[0])
                print(f'  done in {dur:.1f}s  ({fps} gen-fps)  VRAM {vram}GB  RAM {ram}GB')
                writer.writerow([ti, p['caption'], p['type'], si, seed,
                                  round(dur, 2), fps, vram, ram, out_path, 'ok'])
                generated += 1
            else:
                print(f'  ERROR - no mp4 found')
                writer.writerow([ti, p['caption'], p['type'], si, seed,
                                  '', '', vram, ram, '', 'error'])
                errors += 1

            stats_f.flush()
            done += 1

        shutil.rmtree(tmp_dir, ignore_errors=True)
        cap_file.unlink(missing_ok=True)

    stats_f.close()
    elapsed_m = round((time.time() - t_start) / 60, 1)
    print(f'\n[vbench] done - generated={generated}  skipped={skipped}  errors={errors}  elapsed={elapsed_m}m')
    print(f'[vbench] stats -> {stats_path}')


if __name__ == '__main__':
    main()
