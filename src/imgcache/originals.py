from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from imgcache.hash import file_id
from imgcache.spec import SourceSpec


class OriginalsStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, source_file_id: str) -> Path:
        return self.root / source_file_id

    def put(self, path: str | Path, *, mime: str | None = None) -> SourceSpec:
        source_path = Path(path)
        source_file_id = file_id(source_path)
        stored_path = self.path_for(source_file_id)
        if not stored_path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{source_file_id}.", dir=self.root)
            tmp_path = Path(tmp_name)
            os.close(fd)
            try:
                shutil.copyfile(source_path, tmp_path)
                os.replace(tmp_path, stored_path)
            except BaseException:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                raise

        return SourceSpec(
            file_id=source_file_id,
            original_path=stored_path.as_posix(),
            mime=mime,
        )
