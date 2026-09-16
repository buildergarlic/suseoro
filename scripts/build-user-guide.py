# /// script
# requires-python = ">=3.11"
# dependencies = ["Markdown==3.8.2"]
# ///
"""Build the offline HTML manuals: uv run scripts/build-user-guide.py."""

import argparse
from hashlib import sha256
from html import escape
from pathlib import Path
import re
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile

import markdown
from markdown.extensions.toc import slugify_unicode

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
VERSION = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
CSS = """
:root{color-scheme:light;--ink:#17392f;--muted:#52675f;--paper:#fffcf5;--line:#d9e1d8;--green:#226348}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}
body{margin:0;background:var(--paper);color:var(--ink);font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;font-size:16px;line-height:1.85;word-break:keep-all;overflow-wrap:anywhere}
a{color:#176442;text-underline-offset:4px}a:hover{color:#924d14}a:focus-visible,summary:focus-visible{outline:3px solid #a9510c;outline-offset:5px}
.skip{position:absolute;top:-100px;left:16px;background:white;padding:12px;z-index:5}.skip:focus{top:12px}
.topbar{background:var(--ink);color:white;padding:17px 5vw;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}.topbar a{color:white}.brand{text-decoration:none;font-weight:800;letter-spacing:.02em}.topbar nav{display:flex;gap:20px;font-size:14px;flex-wrap:wrap}
.layout{display:grid;grid-template-columns:250px minmax(0,850px);gap:48px;max-width:1230px;margin:38px auto 72px;padding:0 24px}
aside{min-width:0}aside details{position:sticky;top:24px;max-height:calc(100vh - 48px);overflow:auto;border:1px solid var(--line);border-radius:16px;padding:18px;background:#f1f5ed}
summary{cursor:pointer;font-weight:bold}.toc ul{padding-left:20px;list-style:none}.toc>ul{padding:0}.toc li{margin:9px 0;line-height:1.6;font-size:13px}.toc a{text-decoration:none}
main{min-width:0}h1{font-size:clamp(30px,4vw,44px);line-height:1.3;letter-spacing:-.04em;margin:8px 0 24px}h2{font-size:27px;line-height:1.45;letter-spacing:-.03em;margin:60px 0 20px;padding-top:20px;border-top:2px solid var(--line)}h3{font-size:21px;margin:30px 0 12px;line-height:1.5}h4{font-size:18px}
p,ul,ol{margin:14px 0}li{margin:7px 0}strong{color:#163e2e}blockquote{margin:24px 0;background:#eef4e7;border-left:5px solid #43835b;padding:8px 22px;border-radius:0 12px 12px 0}blockquote p{margin:10px 0}
.table-wrap{overflow-x:auto;margin:22px 0;border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:14px;text-align:left}th,td{padding:12px 15px;vertical-align:top;border-bottom:1px solid var(--line)}th{background:#e8efe2}tr:last-child td{border-bottom:0}
code{font-family:Consolas,monospace;font-size:.9em;background:#edf0e8;border-radius:4px;padding:2px 5px;word-break:break-word}pre{overflow:auto;padding:18px;background:#edf0e8;border-radius:12px}pre code{padding:0}hr{border:0;border-top:1px solid var(--line);margin:32px 0}.meta{font-size:13px;color:var(--muted)}
footer{border-top:1px solid var(--line);padding:26px 24px;text-align:center;font-size:13px;color:var(--muted)}.print-button{font:inherit;border:1px solid #bdd1ba;background:white;color:var(--ink);border-radius:9px;padding:7px 13px;cursor:pointer}.print-button:focus-visible{outline:3px solid #a9510c;outline-offset:3px}
@media(max-width:850px){.layout{display:block;margin-top:24px;padding:0 20px}aside details{position:static;max-height:260px;margin-bottom:30px}h2{font-size:24px;margin-top:44px}.topbar nav{gap:14px}.table-wrap{word-break:normal}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{@page{size:A4;margin:16mm 15mm}body{background:white;font-size:10pt;line-height:1.6;color:black}.topbar,aside,.skip,.print-button,footer{display:none}.layout{display:block;max-width:none;margin:0;padding:0}h1{font-size:24pt}h2{font-size:17pt;margin:26px 0 12px;break-after:avoid}h3{font-size:13pt;break-after:avoid}a{color:inherit;text-decoration:none}blockquote{border:1px solid #aaa;background:none}table{font-size:9pt}.table-wrap{overflow:visible}tr{break-inside:avoid}code{font-size:9pt}p,li{orphans:3;widows:3}}
"""


def local_links(content: str) -> str:
    """Keep supplied guide links usable after unzipping the offline manual."""
    return re.sub(
        r'href="(user-guide|quick-start|school-templates)\.md(#[^"]*)?"',
        lambda match: f'href="{match[1]}.html{match[2] or ""}"',
        content,
    )


def build(source: Path) -> None:
    text = source.read_text(encoding="utf-8")
    title = next(line[2:] for line in text.splitlines() if line.startswith("# "))
    # The HTML has a sidebar TOC; keep the explicit TOC only in the Markdown source.
    text = re.sub(r"(?ms)^## 목차\n.*?(?=^## \d)", "", text)
    converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-2", "slugify": slugify_unicode}},
    )
    content = local_links(converter.convert(text))
    content = content.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    page = f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="학교도서관 사서를 위한 수서로 {VERSION} 사용설명서. 글자 크기, 책 표지, ISBN 추가, 추천 목록 정리, 예산, 발주서, 백업을 순서대로 안내합니다.">
<title>{escape(title)} | 수서로 도움말</title><style>{CSS}</style></head>
<body><a class="skip" href="#main">본문으로 바로 가기</a>
<header class="topbar"><a class="brand" href="index.html">수서로 {VERSION} · 도움말</a><nav aria-label="설명서 메뉴"><a href="visual-guide.html">그림으로 익히기</a><a href="user-guide.html">상세 설명서</a><a href="quick-start.html">간단한 사용법</a></nav></header>
<div class="layout"><aside aria-label="이 페이지 목차"><details open><summary>목차</summary>{converter.toc}</details></aside>
<main id="main"><p class="meta">수서로 {VERSION} 기준 · 인터넷 없이 읽을 수 있는 설명서</p><button class="print-button" onclick="window.print()" type="button">인쇄 / PDF로 보관</button>{content}</main></div>
<footer>수서로 · 학교도서관 사서의 도서 구입을 돕습니다. <a href="index.html">도움말 처음으로</a></footer></body></html>
'''
    target = source.with_suffix(".html")
    target.write_text(page, encoding="utf-8", newline="\n")
    print(f"Generated {target.relative_to(ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", action="store_true", help="Also package the offline manuals in dist")
    args = parser.parse_args()
    for name in ("user-guide.md", "quick-start.md", "school-templates.md"):
        build(DOCS / name)
    if args.zip:
        files = [DOCS / name for name in (
            "index.html", "visual-guide.html", "user-guide.html", "quick-start.html", "school-templates.html",
            "user-guide.md", "quick-start.md", "school-templates.md",
        )]
        for source in files:
            if not source.is_file():
                raise FileNotFoundError(source)
        target = ROOT / "dist" / f"Suseoro-Guide-{VERSION}.zip"
        target.parent.mkdir(exist_ok=True)
        with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
            for source in files:
                archive.write(source, source.name)
            archive.write(ROOT / "LICENSE", "LICENSE")
            archive.writestr("먼저 읽어주세요.txt", f"""수서로 {VERSION} 사용설명서

1. 이 ZIP 파일의 압축을 모두 풀어 주세요.
2. index.html을 더블클릭하면 설명서가 열립니다.
3. 그림으로 익히기는 visual-guide.html, 상세 설명서는 user-guide.html입니다.

설명서의 글, 그림, 예산 연습은 인터넷 없이 열립니다.
파일을 각각 옮기지 말고 같은 폴더에 두면 서로 연결됩니다.
프로그램 내려받기와 외부 사이트 방문에는 인터넷이 필요합니다.
인쇄하려면 설명서의 인쇄 버튼 또는 브라우저 인쇄 메뉴를 사용하세요.

그림과 연습용 도서는 이해를 돕기 위한 예시입니다.
연습 결과는 실제 프로그램에 저장되지 않으며 발주서도 전송하지 않습니다.
이 ZIP은 설명서입니다. 프로그램 설치 파일은 GitHub에서 따로 받으세요.

온라인 설명서: https://buildergarlic.github.io/suseoro/
프로그램: https://github.com/buildergarlic/suseoro/releases/latest
그림 설명에 적용한 eli5: https://github.com/anthropics/claude-plugins-community/tree/a727be1c7bd6064419b6f60d71993a19198adc17/eli5
오픈소스 라이선스: 동봉한 LICENSE (AGPL-3.0)
""")
        digest = sha256(target.read_bytes()).hexdigest()
        target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
        print(f"Packaged {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes)")
