"""사람이 귀로 최종 선택할 때 쓰는 로컬 HTML 청취 페이지 생성."""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SHOWN_METRICS = ("effective_duration_s", "peak_dbfs", "integrated_lufs", "attack_ms", "decay_ms",
                 "spectral_centroid_hz", "low_ratio", "high_ratio", "channels")


def _src(page: Path, audio: Path) -> str:
    """페이지 기준 상대 경로(같은 드라이브) 또는 file:// URI."""
    try:
        return Path(os.path.relpath(audio, page.parent)).as_posix()
    except ValueError:  # Windows 에서 드라이브가 다르면 relpath 불가
        return audio.as_uri()


def build(page: Path, title: str, items: list[dict[str, Any]], note: str | None = None) -> Path:
    """items: [{path, analysis, record}] — record 는 manifest 항목(없을 수 있음)."""
    cards = []
    for i, item in enumerate(items, 1):
        audio = Path(item["path"])
        m = item["analysis"]["metrics"]
        interp = item["analysis"]["interpretation"]
        rec = item.get("record") or {}
        params = rec.get("params", {})
        prompt = params.get("prompt") or rec.get("description") or ""
        metric_rows = "".join(f"<tr><th>{k}</th><td>{html.escape(str(m.get(k)))}</td></tr>" for k in SHOWN_METRICS)
        interp_items = "".join(f"<li>{html.escape(v)}</li>" for v in interp.values())
        cards.append(f"""
<section class="card">
  <h2>#{i} {html.escape(audio.name)}</h2>
  <audio controls preload="metadata" src="{html.escape(_src(page, audio))}"></audio>
  <p class="prompt">{html.escape(prompt)}</p>
  <details><summary>파라미터 · 기록</summary>
    <pre>{html.escape(json.dumps({"tool": rec.get("tool"), "params": params, "id": rec.get("id")}, ensure_ascii=False, indent=2))}</pre>
  </details>
  <table>{metric_rows}</table>
  <ul>{interp_items}</ul>
</section>""")
    note_html = f"<p class='note'>{html.escape(note)}</p>" if note else ""
    doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#fafafa; --fg:#1d1d1f; --card:#fff; --line:#e3e3e6; --muted:#6b6b70; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141416; --fg:#ececf0; --card:#1e1e22; --line:#33333a; --muted:#9a9aa2; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }}
.card h2 {{ font-size:15px; margin:0 0 8px; word-break:break-all; }}
audio {{ width:100%; }}
.prompt {{ color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }}
th {{ text-align:left; font-weight:500; color:var(--muted); padding:2px 8px 2px 0; }}
td {{ text-align:right; font-variant-numeric:tabular-nums; }}
ul {{ font-size:13px; padding-left:18px; }}
pre {{ font-size:12px; white-space:pre-wrap; }}
</style></head>
<body><main>
<h1>{html.escape(title)}</h1>
<p class="note">생성 {datetime.now():%Y-%m-%d %H:%M} · 후보 {len(items)}개</p>
{note_html}
<div class="grid">{''.join(cards)}</div>
</main></body></html>"""
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(doc, encoding="utf-8")
    return page
