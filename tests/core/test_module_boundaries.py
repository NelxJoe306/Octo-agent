from __future__ import annotations

import ast
from pathlib import Path
import unittest


class ModuleBoundaryTests(unittest.TestCase):
    def test_lower_layers_do_not_import_terminal_or_higher_level_modules(self):
        source=Path(__file__).resolve().parents[2]/'src'
        forbidden={
            'octo_agent':{'octo_ai','octo_memory','octo_coding'},
            'octo_ai':{'octo_memory','octo_coding'},
            'octo_memory':{'octo_coding'},
        }
        for package,blocked in forbidden.items():
            for path in (source/package).rglob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names=([node.module] if isinstance(node,ast.ImportFrom) else
                           [alias.name for alias in node.names] if isinstance(node,ast.Import) else [])
                    for name in names:
                        if name:
                            self.assertNotIn(name.split('.')[0],blocked,f'{path}:{node.lineno} imports {name}')


if __name__=='__main__':
    unittest.main()
