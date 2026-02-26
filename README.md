# LMSFileGenie
An AI-driven automation tool for LM Studio that reads assistant messages and generates, modifies, or manages files and folders safely within conversation directories.

> ⚠️ **Note:** This project was created as a 12-hour coding challenge entirely using **ChatGPT 5 Mini**. The assistant handled all reasoning, design, and implementation.  
> All code, including French comments and prompts, comes from ChatGPT. The organizer of the project prompted and guided the process but did **not** write or modify any of the code directly.  

## License & Usage

- Anyone can **use, copy, modify, and redistribute** this script freely.  
- You do **not** need to credit the organizer or ChatGPT as a developer.  
- If you credit anyone, it should be as the **organizer of the project**, not a developer.  

---

## Overview

This script monitors **LM Studio conversation JSON files** (typically stored in `~/.lmstudio/conversations`) and executes commands written by the assistant inside the messages. It acts as a **file-based automation engine** and supports advanced operations with **sandboxing and safety measures**.

It is fully written in Python 3 and includes French comments and logging messages because the prompts used were in French.

---

## Features

### 1. File Monitoring

- Monitors either:
  - A single `.json` conversation file.
  - An entire conversations folder.
- Uses **watchdog** if available, with a **polling fallback** if not.
- Detects new or modified messages automatically.

### 2. Queue & Task System

- Commands are enqueued to a **single worker thread**, ensuring sequential execution.
- Avoids race conditions when multiple commands are triggered in quick succession.

### 3. Command Parsing

- Extracts commands from assistant messages in the form `/command arg1 arg2 ...`.
- Supports **fenced code blocks** with ``` for multiline arguments.
- Handles quoted arguments and escapes.

### 4. File Operations (Sandboxed)

All operations are restricted to the conversation folder. The script prevents access outside this directory.

Supported commands:

- **/create_file "name" "content"** → create a new file with optional content.
- **/create_folder "folder_name"** → create a folder.
- **/set line N "path" "new line"** → replace a line in a file.
- **/append "path" "content"** → append content to a file.
- **/replace "path" "old" "new"** → replace text inside a file.
- **/delete_file "path"** → delete a file (with timestamped backup).
- **/delete_folder "path"** → move folder to local `.trash` folder.
- **/remove_line N "path"** → remove a specific line.
- **/move_file "src" "dst"** → move/rename a file.
- **/copy_file "path"** → copy file content into internal clipboard.
- **/paste_file "dst"** → paste content from internal clipboard.
- **/patch "path" ```patch lines```** → apply line-based patches with + (insert) and - (remove) operations.

### 5. External Commands

- **/cmd "command"** executes limited external commands matching a whitelist (e.g., `pip install package`).
- Runs safely using `subprocess.run` with a timeout.

### 6. Plugins

- Plugins are Python scripts in `./plugins` that export a `register()` function.
- Each plugin can define new commands without modifying the core script.
- Handlers receive `(args: List[str], base_dir: Path)` for full control over conversation folder operations.

### 7. Backups & Atomic Writes

- Before modifying or deleting files, the script creates **timestamped backups** (`.bak.YYYYMMDDHHMMSS`).
- Supports **atomic writes** to prevent corruption.
- Optionally keeps or deletes backups via configuration (`KEEP_BACKUPS`).

### 8. State Persistence

- Keeps track of processed messages and content hashes in `.lm_commands_state.json`.
- Ensures commands are executed **only once per message**.

### 9. Logging

- Logs info/warnings/errors with timestamps.
- French messages are used internally as they were part of the original prompts.

---

## Running the Script

```bash
python3 lm_command.py [--conv path/to/file.json] [--folder path/to/conversations] [--poll 2.0]
