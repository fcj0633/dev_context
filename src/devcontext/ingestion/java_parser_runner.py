from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from devcontext.config import project_root
from devcontext.models import Chunk
from devcontext.code_graph.models import AnalysisContractError, CodeSymbol, SymbolEdge, JavaAnalysisResult


class JavaParserRunner:
    def __init__(self, java_project: Path | None = None) -> None:
        self.java_project = java_project or project_root() / "java-parser"
        self.jar_path = self.java_project / "target" / "devcontext-java-parser.jar"

    def build(self) -> None:
        maven = shutil.which("mvn.cmd") or shutil.which("mvn")
        if maven is None:
            raise FileNotFoundError("Maven executable was not found on PATH")
        subprocess.run(
            [maven, "-q", "package"],
            cwd=self.java_project,
            check=True,
        )
        if not self.jar_path.is_file():
            raise FileNotFoundError(f"Parser JAR was not created: {self.jar_path}")

    def parse(self, code_root: Path, output: Path, repository: str) -> list[Chunk]:
        self.build()
        java = shutil.which("java.exe") or shutil.which("java")
        if java is None:
            raise FileNotFoundError("Java executable was not found on PATH")
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                java,
                "-jar",
                str(self.jar_path),
                "--code-root",
                str(code_root),
                "--output",
                str(output),
                "--repository",
                repository,
            ],
            cwd=project_root(),
            check=True,
        )
        chunks: list[Chunk] = []
        with output.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    try:
                        chunks.append(Chunk.from_dict(json.loads(line)))
                    except Exception as exception:
                        raise ValueError(
                            f"Invalid Java parser output at line {line_number}"
                        ) from exception
        return chunks

    def analyze(self, code_root: Path, output: Path, repository: str) -> JavaAnalysisResult:
        self.build()
        java = shutil.which("java.exe") or shutil.which("java")
        if java is None:
            raise FileNotFoundError("Java executable was not found on PATH")
        output.parent.mkdir(parents=True, exist_ok=True)
        symbols_output = output.with_name(output.stem + "-symbols.jsonl")
        edges_output = output.with_name(output.stem + "-edges.jsonl")
        diagnostics_output = output.with_name(output.stem + "-diagnostics.json")
        subprocess.run([
            java, "-jar", str(self.jar_path), "--code-root", str(code_root),
            "--output", str(output), "--repository", repository,
            "--symbols-output", str(symbols_output), "--edges-output", str(edges_output),
            "--diagnostics-output", str(diagnostics_output),
        ], cwd=project_root(), check=True)

        def read_records(path, model):
            records = []
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    records.append(model(**json.loads(line)))
                except (ValueError, TypeError) as error:
                    raise AnalysisContractError(f"Invalid {path.name} at line {number}") from error
            return records

        result = JavaAnalysisResult(
            read_records(output, Chunk), read_records(symbols_output, CodeSymbol),
            read_records(edges_output, SymbolEdge), json.loads(diagnostics_output.read_text(encoding="utf-8")),
        )
        result.validate(repository)
        return result
