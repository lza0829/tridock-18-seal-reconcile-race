import os
import sys
import tempfile
from pathlib import Path

# 必须在任何 app.* 导入之前完成路径与测试库指向
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
_DB_DIR = Path(tempfile.mkdtemp(prefix="tridock-overview-test-"))
os.environ["TRIDOCK_DB_PATH"] = str(_DB_DIR / "test.db")
