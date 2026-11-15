from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from octo_coding.cli import app

if __name__ == '__main__':
    app()
