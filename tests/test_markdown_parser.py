from pathlib import Path

from devcontext.ingestion.markdown_parser import parse_markdown_file, parse_markdown_tree


def test_preserves_preamble_and_nested_heading_paths(tmp_path: Path) -> None:
    document = tmp_path / "design.md"
    document.write_text(
        "项目导言\n\n# 架构设计\n概述\n\n## 订单模块\n事务边界说明\n",
        encoding="utf-8",
    )

    chunks = parse_markdown_file(tmp_path, document)

    assert [chunk.title for chunk in chunks] == ["design", "架构设计", "订单模块"]
    assert chunks[0].heading_path == []
    assert chunks[2].heading_path == ["架构设计", "订单模块"]
    assert chunks[2].start_line == 7
    assert chunks[2].end_line == 7


def test_splits_oversized_section_without_losing_text(tmp_path: Path) -> None:
    document = tmp_path / "long.md"
    body = "第一段内容\n\n" + ("x" * 25) + "\n"
    document.write_text("# 标题\n" + body, encoding="utf-8")

    chunks = parse_markdown_file(tmp_path, document, max_chars=10)

    assert len(chunks) >= 4
    reconstructed = "".join(chunk.content.replace("\n", "") for chunk in chunks)
    assert "第一段内容" in reconstructed
    assert reconstructed.count("x") == 25


def test_excludes_third_party_markdown(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "README.md").write_text("# Project\nUseful", encoding="utf-8")
    excluded = tmp_path / "bower_components" / "lib"
    excluded.mkdir(parents=True)
    (excluded / "README.md").write_text("# Third party", encoding="utf-8")
    theme = tmp_path / "baseline" / "sbadmin2-1.0.7"
    theme.mkdir(parents=True)
    (theme / "README.md").write_text("# Bundled theme", encoding="utf-8")

    chunks = parse_markdown_tree(tmp_path)

    assert len(chunks) == 1
    assert chunks[0].file_path == "project/README.md"
