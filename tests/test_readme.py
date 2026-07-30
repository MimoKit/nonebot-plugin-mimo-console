from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "nonebot_plugin_mimo_console" / "readme.py"
)
spec = importlib.util.spec_from_file_location("mimo_console_readme_test", MODULE_PATH)
assert spec and spec.loader
readme_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readme_module)
render_readme_html = readme_module.render_readme_html


def test_render_readme_supports_github_style_structures() -> None:
    markdown = """\
| 名称 | 状态 |
| :--- | ---: |
| parser | **正常** |

1. 一级项目
   - 嵌套项目

> 引用内容

```python
print("ok")
```

~~已删除~~
"""

    html = render_readme_html(markdown)

    assert "<table>" in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert '<th style="text-align:left">名称</th>' in html
    assert '<th style="text-align:right">状态</th>' in html
    assert "<strong>正常</strong>" in html
    assert "<ol>" in html
    assert "<ul>" in html
    assert "<blockquote>" in html
    assert '<code class="language-python">' in html
    assert "<s>已删除</s>" in html


def test_render_readme_sanitizes_untrusted_html() -> None:
    # Third-party README HTML is untrusted; render() must strip scripts, event
    # handlers, javascript: URLs and unlisted attributes server-side rather than
    # relying only on the client sanitizer.
    html = render_readme_html(
        "<script>alert(1)</script>"
        '<img src="x" onerror="alert(1)">'
        '<a href="javascript:alert(1)">x</a>'
        '<p align="center" style="background:url(//evil)">hi</p>'
    )

    assert "<script>" not in html
    assert "alert(1)" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    # Safe layout attributes survive while dangerous inline styles are dropped.
    assert 'align="center"' in html
    assert "background" not in html


def test_render_readme_keeps_table_alignment_styles() -> None:
    # Alignment is the one inline style GitHub tables need; it must survive while
    # arbitrary styles are stripped.
    html = render_readme_html("| a | b |\n| :--- | ---: |\n| 1 | 2 |\n")

    assert '<th style="text-align:left">a</th>' in html
    assert '<th style="text-align:right">b</th>' in html


def test_render_readme_preserves_safe_html_layout_features() -> None:
    html = render_readme_html(
        '<p align="center">'
        '<img src="https://nonebot.dev/logo.png" width="200" height="200">'
        "</p>"
        "<details open><summary>更多</summary><kbd>Ctrl</kbd></details>"
    )

    assert '<p align="center">' in html
    assert 'width="200"' in html
    assert 'height="200"' in html
    assert '<details open="">' in html
    assert "<summary>更多</summary>" in html
    assert "<kbd>Ctrl</kbd>" in html


def test_render_readme_rejects_unsafe_layout_attribute_values() -> None:
    html = render_readme_html(
        '<p align="expression">x</p>'
        '<img src="https://example.com/x.png" width="999999" height="-1">'
        '<td colspan="999">x</td>'
    )

    assert "expression" not in html
    assert "999999" not in html
    assert 'height="-1"' not in html
    assert 'colspan="999"' not in html
