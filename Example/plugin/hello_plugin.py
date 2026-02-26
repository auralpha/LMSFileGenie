# ./plugins/hello_plugin.py
from pathlib import Path

def register():
    return {
        "say_hello": say_hello
    }

def say_hello(args, base_dir: Path):
    file_path = base_dir / "hello.txt"
    content = "Hello from plugin\n"
    file_path.write_text(content, encoding="utf-8")
    print(f"Fichier créé par plugin: {file_path}")