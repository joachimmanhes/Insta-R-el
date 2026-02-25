from __future__ import annotations
import json, subprocess, os
from pathlib import Path

def sh(cmd: list[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stdout)
    return p.stdout

def load_payload() -> dict:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if "client_payload" in event:  # repository_dispatch
        return event["client_payload"]
    if "inputs" in event and "payload" in event["inputs"]:  # manual
        return json.loads(event["inputs"]["payload"])
    raise RuntimeError("No client_payload found")

def main():
    payload = load_payload()
    url = payload["video_url"]

    out_dir = Path("out"); out_dir.mkdir(exist_ok=True)
    work = Path("work"); work.mkdir(exist_ok=True)

    src = work / "source.mp4"

    # Download (only if you have rights)
    sh(["yt-dlp","-f","bv*+ba/b","--merge-output-format","mp4","-o",str(src),url])

    # Transcribe (Whisper -> JSON with segments/timestamps)
    sh(["python","-m","whisper",str(src),"--model","base","--task","transcribe","--output_format","json","--output_dir",str(work)])

    # Whisper output file name varies; grab the first json
    wjson = next(work.glob("source*.json"))
    data = json.loads(wjson.read_text(encoding="utf-8"))
    segments = data.get("segments", [])

    # Build candidate windows (~35s) and score them
    WIN = 35.0
    candidates = []
    i = 0
    while i < len(segments):
        start = float(segments[i]["start"])
        end_target = start + WIN

        txt = []
        j = i
        end_actual = start
        while j < len(segments) and float(segments[j]["end"]) <= end_target:
            txt.append(str(segments[j]["text"]).strip())
            end_actual = float(segments[j]["end"])
            j += 1

        if j == i:
            i += 1
            continue

        text = " ".join(txt)
        lower = text.lower()

        # Heuristics (simple but effective)
        kw = ["erreur","astuce","secret","important","attention","3","5","never","mistake","tip","hack"]
        score = sum(1 for k in kw if k in lower) * 2
        score += min(len(text) / 200.0, 3.0)  # density bonus

        candidates.append({
            "start": start,
            "end": end_actual,
            "text": text,
            "score": score
        })
        i = j

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:3] if candidates else []

    # Render top clips vertical 9:16 with burned subtitles
    for idx, c in enumerate(top, start=1):
        clip_start = float(c["start"])
        clip_end = float(c["end"])

        # Build SRT from overlapping segments (shifted to 0)
        srt_lines = []
        n = 1

        def ts(t: float) -> str:
            ms = int(round(t * 1000))
            hh = ms // 3600000; ms -= hh * 3600000
            mm = ms // 60000; ms -= mm * 60000
            ss = ms // 1000; ms -= ss * 1000
            return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"

        for s in segments:
            s_start = float(s["start"])
            s_end = float(s["end"])
            if s_end <= clip_start or s_start >= clip_end:
                continue
            ss = max(s_start, clip_start) - clip_start
            ee = min(s_end, clip_end) - clip_start
            text = str(s["text"]).strip()
            if not text:
                continue
            srt_lines += [str(n), f"{ts(ss)} --> {ts(ee)}", text, ""]
            n += 1

        srt_path = work / f"clip_{idx}.srt"
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        out_mp4 = out_dir / f"clip_{idx}.mp4"

        vf = (
            "scale=-2:1920,crop=1080:1920,"
            f"subtitles={str(srt_path)}:force_style='Fontsize=54,Outline=2,MarginV=120'"
        )

        sh([
            "ffmpeg","-y",
            "-ss", str(clip_start), "-to", str(clip_end),
            "-i", str(src),
            "-vf", vf,
            "-c:v","libx264","-preset","veryfast","-crf","22",
            "-c:a","aac","-b:a","128k",
            "-movflags","+faststart",
            str(out_mp4)
        ])

    # Report
    (out_dir / "report.json").write_text(
        json.dumps({"video_url": url, "clips": top}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

if __name__ == "__main__":
    main()
