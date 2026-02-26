#!/usr/bin/env python3
# lm_command.py  (version enrichie : queue, watchdog, plugins, sandbox, commandes supplémentaires)
"""
Surveille LM Studio (~\.lmstudio\conversations) et exécute des commandes
fournies par les messages assistant. Améliorations :
- file queue (exécution séquentielle)
- watchdog si disponible (fallback polling)
- plugins dans ./plugins (fichier .py exportant register() -> dict)
- sandbox strict : aucune opération en dehors du dossier du conversation
- commandes supplémentaires : delete_file, delete_folder, remove_line, move_file,
  copy_file, paste_file, patch, cmd (restreint)
Tourne jusqu'à Ctrl+C.
"""

from pathlib import Path
import os
import json
import time
import argparse
import logging
import hashlib
import re
import shutil
import datetime
import threading
import queue
import shlex
import subprocess
from typing import Callable, Dict, List, Any, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE_FILE = Path(".lm_commands_state.json")

# Config
KEEP_BACKUPS = False          # garder les backups .bak.* (timestamped)
ATOMIC_WRITE = True           # écrire via fichier temporaire + replace
USE_WATCHDOG = True           # tenter d'utiliser watchdog (si dispo)
PLUGINS_DIR = Path("./plugins")
ALLOWED_FILE_EXTENSIONS = {".py", ".js", ".json", ".md", ".txt", ""}  # "" = pas d'extension allowed for scripts
CMD_WHITELIST_PATTERNS = [
    # autorise : pip install package, pip3 install package, python -m pip install package
    r"^pip(?:3)?\s+install\s+[A-Za-z0-9_.\-\[\]\(\)]+(?:==[0-9A-Za-z.+-]+)?$",
    r"^python(?:3)?\s+-m\s+pip\s+install\s+[A-Za-z0-9_.\-\[\]\(\)]+(?:==[0-9A-Za-z.+-]+)?$",
]
# Timeout for external /cmd
CMD_TIMEOUT = 60  # secondes

# try to import watchdog
WATCHDOG_AVAILABLE = False
if USE_WATCHDOG:
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
        WATCHDOG_AVAILABLE = True
        logging.info("watchdog disponible -> utilisé pour événement FS.")
    except Exception:
        WATCHDOG_AVAILABLE = False
        logging.info("watchdog non disponible -> fallback polling sera utilisé.")


# ---------------- Helpers ------------------------------------------------
def get_lmstudio_conversations_folder():
    return Path(os.path.expanduser(r"~\.lmstudio\conversations"))

def sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def ensure_parent_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)

def atomic_write(path: Path, content: str):
    """Write content atomically (via tmp then replace)."""
    ensure_parent_dir(path)
    if not ATOMIC_WRITE:
        path.write_text(content, encoding="utf-8")
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # use replace for atomic move
    tmp.replace(path)

def make_timestamped_backup(path: Path) -> Path:
    if not path.exists():
        return None
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(path.name + f".bak.{ts}")
    try:
        shutil.copy(str(path), str(backup))
        logging.info(f"Sauvegarde créée: {backup}")
        return backup
    except Exception as e:
        logging.warning(f"Impossible de créer la sauvegarde {backup}: {e}")
        return None

def remove_file_safe(path: Path):
    try:
        path.unlink()
        logging.info(f"Supprimé : {path}")
    except Exception as e:
        logging.error(f"Impossible de supprimer {path}: {e}")

def path_in_sandbox(target: Path, base_dir: Path) -> bool:
    """Vérifie que target est dans base_dir (après resolve)."""
    try:
        target_r = target.resolve()
        base_r = base_dir.resolve()
        # Python 3.9+: Path.is_relative_to, mais on utilise try/except pour compatibilité
        try:
            return target_r.is_relative_to(base_r)
        except Exception:
            try:
                target_r.relative_to(base_r)
                return True
            except Exception:
                return False
    except Exception:
        return False

# ---------------- Content sanitation -----------------------------------
def _normalize_content_for_writing_from_fence(content: str) -> str:
    if content is None:
        return ""
    if content.startswith("\n"):
        content = content[1:]
    return content

def _strip_command_lines(content: str) -> str:
    if content is None:
        return ""
    lines = content.splitlines()
    kept = []
    for ln in lines:
        if re.match(r'^\s*/[a-zA-Z_]\w*', ln):
            logging.debug(f"Suppression d'une ligne-commande du contenu: {ln!r}")
            continue
        kept.append(ln)
    return "\n".join(kept)

def _content_has_command_lines(content: str) -> bool:
    if not content:
        return False
    for ln in content.splitlines():
        if re.match(r'^\s*/[a-zA-Z_]\w*', ln):
            return True
    return False

# ---------------- Plugin loader -----------------------------------------
def load_plugins(command_handlers: Dict[str, Callable]):
    PLUGINS_DIR.mkdir(exist_ok=True)
    for p in PLUGINS_DIR.glob("*.py"):
        try:
            # import plugin as module by path
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"lm_plugin_{p.stem}", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register") and callable(getattr(mod, "register")):
                reg = mod.register()
                if isinstance(reg, dict):
                    for k, v in reg.items():
                        if callable(v) and k not in command_handlers:
                            command_handlers[k] = v
                            logging.info(f"Plugin {p.name} -> commande /{k} enregistrée.")
            else:
                logging.debug(f"Plugin {p.name} n'exporte pas register(). Ignoré.")
        except Exception as e:
            logging.warning(f"Erreur chargement plugin {p}: {e}")

# ---------------- Command parsing (``` fences supported) -------------
def find_commands_in_text(text: str):
    """Retourne liste (cmd, args) ; fences ``` sont traités comme args multi-lignes."""
    if not text:
        return []
    commands = []
    i = 0
    L = len(text)
    while True:
        m = text.find("/", i)
        if m == -1:
            break
        if m > 0 and text[m-1] not in ("\n", "\r"):
            i = m + 1
            continue
        cmd_match = re.match(r"/([a-zA-Z_]\w*)", text[m:])
        if not cmd_match:
            i = m + 1
            continue
        cmd = cmd_match.group(1)
        pos = m + cmd_match.end()
        args = []
        while True:
            while pos < L and text[pos].isspace():
                pos += 1
            if pos >= L or text[pos] == "/":
                break
            ch = text[pos]
            if ch == '`' and pos + 2 < L and text[pos:pos+3] == '```':
                pos_start = pos + 3
                newline_idx = text.find("\n", pos_start)
                if newline_idx != -1:
                    end_idx = text.find("```", newline_idx + 1)
                    if end_idx == -1:
                        val = text[newline_idx+1:]
                        pos = L
                    else:
                        val = text[newline_idx+1:end_idx]
                        pos = end_idx + 3
                else:
                    end_idx = text.find("```", pos_start)
                    if end_idx == -1:
                        val = text[pos_start:]
                        pos = L
                    else:
                        val = text[pos_start:end_idx]
                        pos = end_idx + 3
                args.append(val)
                continue
            if ch in ('"', "'"):
                quote = ch
                j = pos + 1
                val_chars = []
                escaped = False
                while j < L:
                    c = text[j]
                    if escaped:
                        val_chars.append(c)
                        escaped = False
                    elif c == "\\":
                        escaped = True
                    elif c == quote:
                        break
                    else:
                        val_chars.append(c)
                    j += 1
                val = "".join(val_chars)
                args.append(val)
                pos = j + 1 if j < L else j
                continue
            j = pos
            token_chars = []
            while j < L and not text[j].isspace() and text[j] != "/":
                token_chars.append(text[j])
                j += 1
            if token_chars:
                args.append("".join(token_chars))
            pos = j
            continue
        commands.append((cmd, args))
        i = pos
    return commands

# ---------------- Command handlers ------------------------------------
# We'll maintain a "clipboard" internal for copy/paste file content
_internal_clipboard = {"content": None, "path": None}

def _safe_target(base_dir: Path, ai_path: str) -> Path:
    """
    Map ai_path (string) to a Path inside base_dir, and ensure sandbox.
    Accepts quoted paths.
    """
    original = ai_path.strip().strip('"').strip("'")
    p = original.replace("\\", "/")
    candidate = base_dir.joinpath(p) if not os.path.isabs(p) else base_dir.joinpath(p.lstrip("/\\"))
    candidate = candidate.resolve() if candidate.exists() else candidate
    if not path_in_sandbox(candidate, base_dir):
        raise PermissionError(f"Target hors sandbox: {ai_path}")
    return candidate

def handle_create_folder(args: List[str], base_dir: Path):
    if not args:
        logging.warning("/create_folder: pas d'arguments")
        return
    folder_name = args[0]
    target = _safe_target(base_dir, folder_name)
    target.mkdir(parents=True, exist_ok=True)
    logging.info(f"Dossier créé : {target}")

def handle_create_file(args: List[str], base_dir: Path):
    if not args:
        logging.warning("/create_file: pas d'arguments")
        return
    name = args[0]
    target = base_dir.joinpath(name)
    # if second arg is a path prefix or content (fence), handle similarly as before
    content = None
    if len(args) >= 2:
        second = args[1]
        if isinstance(second, str) and "\n" in second:
            content = "\n".join([a for a in args[1:] if isinstance(a, str)])
        else:
            # second is a dir or path
            mapped = base_dir.joinpath(second)
            if second.endswith(("/", "\\")) or mapped.is_dir():
                target = mapped.joinpath(name)
            else:
                if mapped.suffix:
                    target = mapped
                else:
                    target = mapped.joinpath(name)
            if len(args) > 2:
                third = args[2]
                if isinstance(third, str) and "\n" in third:
                    content = "\n".join([a for a in args[2:] if isinstance(a, str)])
    # sandbox check
    if not path_in_sandbox(target, base_dir):
        logging.warning(f"/create_file: chemin hors sandbox -> refusé: {target}")
        return
    ensure_parent_dir(target)
    if content is None:
        if not target.exists():
            atomic_write(target, "")
            logging.info(f"Fichier créé (vide) : {target}")
        else:
            logging.info(f"Fichier existe déjà (rien changé) : {target}")
    else:
        content = _normalize_content_for_writing_from_fence(content)
        if _content_has_command_lines(content):
            logging.info(f"Nettoyage lignes-commande dans contenu pour {target}")
            content = _strip_command_lines(content)
        if not content:
            logging.warning(f"Après nettoyage, contenu vide pour {target}. Aucun écrit.")
            return
        # extension whitelist check
        if target.suffix not in ALLOWED_FILE_EXTENSIONS:
            logging.warning(f"Extension non autorisée pour {target} -> refusé")
            return
        backup = make_timestamped_backup(target) if target.exists() else None
        try:
            atomic_write(target, content)
            logging.info(f"Fichier créé/écrit : {target} ({len(content)} octets)")
            if backup and not KEEP_BACKUPS:
                try:
                    backup.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Échec écriture {target}: {e}")

def handle_set(args: List[str], base_dir: Path):
    # support /set line N "path" "nouvelle ligne"
    if not args:
        logging.warning("/set: pas d'arguments")
        return
    if args[0] == "line":
        if len(args) < 4:
            logging.warning('/set line: usage /set line [numéro] "chemin" "nouvelle ligne"')
            return
        try:
            line_num = int(args[1])
        except Exception:
            logging.warning(f"/set line: numéro invalide {args[1]!r}")
            return
        path_arg = args[2]
        new_line = " ".join(args[3:]) if len(args) >= 4 else ""
        target = _safe_target(base_dir, path_arg)
        ensure_parent_dir(target)
        if not target.exists():
            target.write_text("", encoding="utf-8")
        backup = make_timestamped_backup(target)
        # read-modify-write
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        idx = max(0, line_num - 1)
        while len(lines) < idx:
            lines.append("")
        if idx < len(lines):
            lines[idx] = new_line
        else:
            lines.append(new_line)
        new_text = "\n".join(lines) + ("\n" if new_line.endswith("\n") else "")
        try:
            atomic_write(target, new_text)
            logging.info(f"/set line: ligne {line_num} mise à jour dans {target}")
            if backup and not KEEP_BACKUPS:
                try:
                    backup.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Erreur /set line sur {target}: {e}")
            if backup and backup.exists():
                shutil.copy(str(backup), str(target))

    else:
        # standard /set path "content"
        path_arg = args[0]
        content_parts = [a for a in args[1:] if isinstance(a, str) and "\n" in a]
        content = "\n".join(content_parts) if content_parts else " ".join(args[1:]) if len(args) >= 2 else ""
        target = _safe_target(base_dir, path_arg)
        ensure_parent_dir(target)
        if target.exists():
            backup = make_timestamped_backup(target)
        else:
            backup = None
        content = _normalize_content_for_writing_from_fence(content)
        if _content_has_command_lines(content):
            logging.info(f"Nettoyage lignes-commande pour {target}")
            content = _strip_command_lines(content)
        if not content and target.exists():
            logging.info("/set: contenu vide après nettoyage -> aucun changement")
            return
        try:
            atomic_write(target, content)
            logging.info(f"/set: écrit dans {target} ({len(content)} octets)")
            if backup and not KEEP_BACKUPS:
                try:
                    backup.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"Erreur /set sur {target}: {e}")
            if backup and backup.exists():
                shutil.copy(str(backup), str(target))

def handle_append(args: List[str], base_dir: Path):
    if not args:
        logging.warning("/append: pas d'arguments")
        return
    path_arg = args[0]
    content_parts = [a for a in args[1:] if isinstance(a, str) and "\n" in a]
    content = "\n".join(content_parts) if content_parts else " ".join(args[1:]) if len(args) >= 2 else ""
    target = _safe_target(base_dir, path_arg)
    ensure_parent_dir(target)
    content = _normalize_content_for_writing_from_fence(content)
    if _content_has_command_lines(content):
        logging.info(f"Nettoyage lignes-commande pour append -> {target}")
        content = _strip_command_lines(content)
    if not content:
        logging.info("/append: rien à ajouter après nettoyage")
        return
    try:
        # append atomically: read existing then write combined atomically
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        new_text = existing + content
        atomic_write(target, new_text)
        logging.info(f"/append: ajouté à {target} ({len(content)} octets)")
    except Exception as e:
        logging.error(f"Erreur /append sur {target}: {e}")

def handle_replace(args: List[str], base_dir: Path):
    if not args or len(args) < 3:
        logging.warning('/replace: usage /replace "path" "old" "new"')
        return
    path_arg = args[0]
    old = args[1]
    new = " ".join(args[2:])
    target = _safe_target(base_dir, path_arg)
    if not target.exists():
        logging.warning(f"/replace: fichier introuvable {target}")
        return
    old_n = _normalize_content_for_writing_from_fence(old)
    new_n = _normalize_content_for_writing_from_fence(new)
    backup = make_timestamped_backup(target)
    try:
        text = target.read_text(encoding="utf-8")
        new_text = text.replace(old_n, new_n)
        if _content_has_command_lines(new_text):
            logging.info("/replace: nettoyage des éventuelles lignes-commande")
            new_text = _strip_command_lines(new_text)
        atomic_write(target, new_text)
        logging.info(f"/replace effectué dans {target} : {len(text)} -> {len(new_text)} octets")
        if backup and not KEEP_BACKUPS:
            try:
                backup.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"/replace erreur sur {target}: {e}")
        if backup and backup.exists():
            shutil.copy(str(backup), str(target))

# --- NEW: delete_file, delete_folder, remove_line, move_file, copy/paste, patch, cmd

def handle_delete_file(args: List[str], base_dir: Path):
    if not args:
        logging.warning("/delete_file: pas d'arguments")
        return
    target = _safe_target(base_dir, args[0])
    if target.exists() and target.is_file():
        make_timestamped_backup(target)  # backup before delete
        remove_file_safe(target)
    else:
        logging.warning(f"/delete_file: introuvable ou pas un fichier: {target}")

def handle_delete_folder(args: List[str], base_dir: Path):
    if not args:
        logging.warning("/delete_folder: pas d'arguments")
        return
    target = _safe_target(base_dir, args[0])
    if target.exists() and target.is_dir():
        # backup policy: create a zip? -- for simplicity, create timestamped marker and move to .trash inside base_dir
        trash = base_dir.joinpath(".trash")
        trash.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        dest = trash.joinpath(target.name + f".deleted.{ts}")
        try:
            shutil.move(str(target), str(dest))
            logging.info(f"Dossier déplacé en poubelle locale: {dest}")
        except Exception as e:
            logging.error(f"/delete_folder erreur: {e}")
    else:
        logging.warning(f"/delete_folder: introuvable ou pas un dossier: {target}")

def handle_remove_line(args: List[str], base_dir: Path):
    # usage: /remove_line 12 "path"
    if len(args) < 2:
        logging.warning('/remove_line: usage /remove_line [numéro] "chemin"')
        return
    try:
        line_num = int(args[0])
    except Exception:
        logging.warning(f"/remove_line: numéro invalide {args[0]!r}")
        return
    target = _safe_target(base_dir, args[1])
    if not target.exists():
        logging.warning(f"/remove_line: fichier introuvable {target}")
        return
    backup = make_timestamped_backup(target)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
        idx = line_num - 1
        if 0 <= idx < len(lines):
            removed = lines.pop(idx)
            atomic_write(target, "\n".join(lines) + ("\n" if len(lines) and lines[-1].endswith("\n") else ""))
            logging.info(f"/remove_line: ligne {line_num} supprimée dans {target} (contenu: {removed!r})")
        else:
            logging.warning(f"/remove_line: numéro hors limites pour {target}")
    except Exception as e:
        logging.error(f"/remove_line erreur: {e}")
        if backup and backup.exists():
            shutil.copy(str(backup), str(target))

def handle_move_file(args: List[str], base_dir: Path):
    if len(args) < 2:
        logging.warning('/move_file: usage /move_file "src" "dst"')
        return
    src = _safe_target(base_dir, args[0])
    dst = _safe_target(base_dir, args[1])
    if not src.exists():
        logging.warning(f"/move_file: src introuvable {src}")
        return
    ensure_parent_dir(dst)
    try:
        backup = make_timestamped_backup(src)
        shutil.move(str(src), str(dst))
        logging.info(f"/move_file: {src} -> {dst}")
        if backup and not KEEP_BACKUPS:
            try:
                backup.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"/move_file erreur: {e}")

def handle_copy_file(args: List[str], base_dir: Path):
    if len(args) < 1:
        logging.warning('/copy_file: usage /copy_file "path"')
        return
    src = _safe_target(base_dir, args[0])
    if not src.exists() or not src.is_file():
        logging.warning(f"/copy_file: fichier introuvable: {src}")
        return
    try:
        content = src.read_text(encoding="utf-8")
        _internal_clipboard["content"] = content
        _internal_clipboard["path"] = src
        logging.info(f"/copy_file: contenu de {src} copié en mémoire (clipboard interne)")
    except Exception as e:
        logging.error(f"/copy_file erreur: {e}")

def handle_paste_file(args: List[str], base_dir: Path):
    if len(args) < 1:
        logging.warning('/paste_file: usage /paste_file "destination_path"')
        return
    if not _internal_clipboard.get("content"):
        logging.warning("/paste_file: clipboard interne vide")
        return
    dst = _safe_target(base_dir, args[0])
    ensure_parent_dir(dst)
    if dst.suffix not in ALLOWED_FILE_EXTENSIONS:
        logging.warning(f"/paste_file: extension non autorisée pour {dst} -> refusé")
        return
    backup = make_timestamped_backup(dst) if dst.exists() else None
    try:
        atomic_write(dst, _internal_clipboard["content"])
        logging.info(f"/paste_file: contenu collé dans {dst}")
        if backup and not KEEP_BACKUPS:
            try:
                backup.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"/paste_file erreur: {e}")
        if backup and backup.exists():
            shutil.copy(str(backup), str(dst))

def handle_patch(args: List[str], base_dir: Path):
    """
    /patch "path" ``` patch lines ```
    patch lines format per line: "<lineno> <op> <text...>"
    where op is '+' (insert before lineno) or '-' (remove lineno or check match)
    Example:
    ```
    12 - old line text
    15 + new inserted line
    ```
    """
    if len(args) < 2:
        logging.warning('/patch: usage /patch "chemin" ```patch```')
        return
    path_arg = args[0]
    patches = "\n".join(args[1:]) if len(args) > 1 else ""
    target = _safe_target(base_dir, path_arg)
    if not target.exists():
        logging.warning(f"/patch: fichier introuvable {target}")
        return
    backup = make_timestamped_backup(target)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
        # parse patch lines
        ops = []
        for raw in patches.splitlines():
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            m = re.match(r"^\s*(\d+)\s*([+-])\s*(.*)$", raw)
            if not m:
                logging.warning(f"/patch: ligne de patch non comprise: {raw!r} -> ignorée")
                continue
            lineno = int(m.group(1))
            op = m.group(2)
            text = m.group(3)
            ops.append((lineno, op, text))
        # apply ops: to avoid index shifts, sort by lineno descending for inserts/removes?
        # We'll apply removes first (descending), then inserts (ascending)
        removes = [o for o in ops if o[1] == '-']
        inserts = [o for o in ops if o[1] == '+']
        for lineno, op, text in sorted(removes, key=lambda x: x[0], reverse=True):
            idx = lineno - 1
            if 0 <= idx < len(lines):
                # if text provided, check match before removing
                if text and lines[idx].strip() != text.strip():
                    logging.info(f"/patch remove: contenu ligne {lineno} différent -> suppression ignorée")
                else:
                    removed = lines.pop(idx)
                    logging.info(f"/patch: ligne {lineno} supprimée ({removed!r})")
            else:
                logging.warning(f"/patch remove: ligne {lineno} hors limites -> ignorée")
        for lineno, op, text in sorted(inserts, key=lambda x: x[0]):
            idx = lineno - 1
            if idx < 0:
                idx = 0
            # if idx > len(lines) -> append blanks until then
            while len(lines) < idx:
                lines.append("")
            lines.insert(idx, text)
            logging.info(f"/patch: inséré à la ligne {lineno}: {text!r}")
        new_text = "\n".join(lines) + ("\n" if lines and not lines[-1].endswith("\n") else "")
        if _content_has_command_lines(new_text):
            logging.info("/patch: nettoyage lignes-commande éventuelles")
            new_text = _strip_command_lines(new_text)
        atomic_write(target, new_text)
        logging.info(f"/patch appliqué sur {target}")
        if backup and not KEEP_BACKUPS:
            try:
                backup.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"/patch erreur: {e}")
        if backup and backup.exists():
            shutil.copy(str(backup), str(target))

def handle_cmd(args: List[str], base_dir: Path):
    """
    Exécute une commande très limitée (whitelist). Exemple: /cmd "pip install requests"
    Sécurité: on n'autorise que des patterns simples (CMD_WHITELIST_PATTERNS).
    Exécute en subprocess.run avec shell=False.
    """
    if not args:
        logging.warning("/cmd: pas d'arguments")
        return
    cmdline = " ".join(args).strip()
    # validate against whitelist regexes
    ok = False
    for pat in CMD_WHITELIST_PATTERNS:
        if re.match(pat, cmdline):
            ok = True
            break
    if not ok:
        logging.warning(f"/cmd: commande non autorisée -> refusée: {cmdline!r}")
        return
    # run safely: shlex.split and subprocess.run
    try:
        parts = shlex.split(cmdline)
        logging.info(f"/cmd: exécution sécurisée -> {parts}")
        # run with timeout
        res = subprocess.run(parts, cwd=str(base_dir), capture_output=True, text=True, timeout=CMD_TIMEOUT)
        logging.info(f"/cmd: retour code {res.returncode}")
        if res.stdout:
            logging.info(f"/cmd stdout:\n{res.stdout.strip()}")
        if res.stderr:
            logging.warning(f"/cmd stderr:\n{res.stderr.strip()}")
    except subprocess.TimeoutExpired:
        logging.error("/cmd: timeout expiré")
    except Exception as e:
        logging.error(f"/cmd: erreur exécution: {e}")

# Map basic commands to handlers
COMMAND_HANDLERS: Dict[str, Callable[[List[str], Path], None]] = {
    "create_folder": handle_create_folder,
    "create_file": handle_create_file,
    "create_script": handle_create_file,
    "screate_script": handle_create_file,
    "set": handle_set,
    "append": handle_append,
    "replace": handle_replace,
    # new:
    "delete_file": handle_delete_file,
    "delete_folder": handle_delete_folder,
    "remove_line": handle_remove_line,
    "move_file": handle_move_file,
    "copy_file": handle_copy_file,
    "paste_file": handle_paste_file,
    "patch": handle_patch,
    "cmd": handle_cmd,
}

# Load plugins which may register additional commands
load_plugins(COMMAND_HANDLERS)

# ---------------- State persistence -----------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                return {}
            clean = {}
            for k, v in st.items():
                if not isinstance(v, dict):
                    clean[k] = {"hashes": {}, "last_mtime": 0}
                else:
                    hashes = v.get("hashes") if isinstance(v.get("hashes"), dict) else {}
                    last_mtime = float(v.get("last_mtime") or 0)
                    clean[k] = {"hashes": hashes, "last_mtime": last_mtime}
            return clean
        except Exception:
            return {}
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------- File queue worker -----------------------------------
_task_queue = queue.Queue()

def enqueue_task(func: Callable, args: Tuple[Any, ...]):
    _task_queue.put((func, args))

def _worker():
    while True:
        try:
            func, args = _task_queue.get()
            try:
                func(*args)
            except Exception as e:
                logging.error(f"Erreur exécution tâche queue: {e}")
            finally:
                _task_queue.task_done()
        except Exception as e:
            logging.error(f"Worker queue erreur: {e}")
            time.sleep(1)

_worker_thread = threading.Thread(target=_worker, daemon=True)
_worker_thread.start()

# ---------------- Processing updates ----------------------------------
def process_assistant_message_text(text: str, base_dir: Path):
    cmds = find_commands_in_text(text)
    if not cmds:
        return []
    executed = []
    for cmd, args in cmds:
        handler = COMMAND_HANDLERS.get(cmd)
        if not handler:
            logging.warning(f"Commande inconnue ignorée: /{cmd}")
            continue
        # Push into queue to ensure sequential execution and avoid races
        logging.info(f"Enqueue /{cmd} {args}")
        enqueue_task(handler, (args, base_dir))
        executed.append((cmd, args))
    return executed

def extract_text_from_version(version: dict) -> str:
    # (same logic as before but compact)
    parts = []
    steps = version.get("steps")
    if isinstance(steps, list) and steps:
        for step in steps:
            style = step.get("style") or {}
            if isinstance(style, dict) and style.get("type") and str(style.get("type")).lower() == "thinking":
                continue
            sc = step.get("content")
            if isinstance(sc, str):
                parts.append(sc)
            elif isinstance(sc, list):
                for item in sc:
                    if isinstance(item, dict):
                        txt = item.get("text") or item.get("content") or ""
                        parts.append(txt)
        if parts:
            return "\n\n".join([re.sub(r"<think>.*?</think>", "", p, flags=re.DOTALL|re.IGNORECASE).strip() for p in parts]).strip()
    cont = version.get("content")
    if isinstance(cont, str):
        parts.append(cont)
    elif isinstance(cont, list):
        for item in cont:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("content") or ""
                parts.append(txt)
    if not parts:
        t = version.get("text")
        if isinstance(t, str):
            parts.append(t)
    cleaned = "\n\n".join(parts)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL|re.IGNORECASE).strip()
    return cleaned

def extract_text_from_message(msg: dict):
    versions = msg.get("versions") or []
    if not isinstance(versions, list) or len(versions) == 0:
        role = msg.get("role") or msg.get("author") or ""
        content = msg.get("content") or msg.get("text") or ""
        if isinstance(content, list):
            content = "\n".join([_strip_command_lines(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content])
        elif isinstance(content, dict):
            content = content.get("text", "")
        return str(role).lower(), content
    sel = msg.get("currentlySelected")
    version = versions[sel] if isinstance(sel, int) and 0 <= sel < len(versions) else versions[-1]
    role = version.get("role") or ""
    text = extract_text_from_version(version)
    return str(role).lower(), text

TEMP_NAME_REGEX = re.compile(r"^\d{10,}\.conversation$")

def is_temp_conversation_name(name: str) -> bool:
    if not name:
        return False
    return bool(TEMP_NAME_REGEX.match(name.strip()))

def get_or_create_conversation_dir(conv_path: Path, conv_name: str) -> Path:
    """
    Empêche la création de doublons quand le nom passe de temporaire à réel.
    """

    conversations_root = Path.cwd()

    temp_name = conv_path.stem
    temp_dir = conversations_root / temp_name
    final_dir = conversations_root / conv_name

    # si nom final existe déjà → utiliser
    if final_dir.exists():
        return final_dir

    # si temp existe et conv_name n'est pas temporaire → renommer
    if temp_dir.exists() and not is_temp_conversation_name(conv_name):

        try:
            temp_dir.rename(final_dir)
            logging.info(f"Conversation renommée: {temp_dir.name} → {final_dir.name}")
            return final_dir

        except Exception as e:
            logging.warning(f"Rename impossible: {e}")
            return temp_dir

    # si nom est temporaire → utiliser temp
    if is_temp_conversation_name(conv_name):

        temp_dir.mkdir(exist_ok=True)

        return temp_dir

    # sinon créer final
    final_dir.mkdir(exist_ok=True)

    return final_dir

def process_updates_for_file(conv_path: Path, state: dict):
    data = {}
    try:
        with conv_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.warning(f"Impossible de lire JSON {conv_path}: {e}")
        return
    messages = data.get("messages") or []
    if not isinstance(messages, list):
        logging.warning(f"Structure inattendue des messages dans {conv_path}; attendu une list.")
        return
    key = str(conv_path.resolve())
    entry = state.get(key, {"hashes": {}, "last_mtime": 0})
    hashes = entry.get("hashes") if isinstance(entry.get("hashes"), dict) else {}
    conv_name = data.get("name") or data.get("title") or conv_path.stem
    conv_name = conv_name.strip() if isinstance(conv_name, str) else conv_path.stem
    base_dir = get_or_create_conversation_dir(conv_path, conv_name)
    updated = False
    for idx, msg in enumerate(messages):
        role, content = extract_text_from_message(msg)
        # only assistant-like roles
        if role not in ("assistant", "system", "bot", "model"):
            hashes[str(idx)] = sha256_hex(content or "")
            continue
        fp = sha256_hex(content or "")
        prev_fp = hashes.get(str(idx))
        if prev_fp == fp:
            continue
        if content:
            logging.info(f"Traitement message assistant index={idx} dans {conv_path.name}")
            executed = process_assistant_message_text(content, base_dir)
            if executed:
                logging.info(f"Commandes enqueued: {executed}")
        hashes[str(idx)] = fp
        updated = True
    if updated:
        try:
            mtime = float(conv_path.stat().st_mtime)
        except Exception:
            mtime = time.time()
        state[key] = {"hashes": hashes, "last_mtime": mtime}
        save_state(state)

# ---------------- Watchdog / main loop ---------------------------------
class SimpleWatchHandler(FileSystemEventHandler):
    def __init__(self, folder: Path, state: dict):
        self.folder = folder
        self.state = state
    def on_modified(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix.lower() == ".json":
            logging.info(f"watchdog: modification détectée: {p}")
            process_updates_for_file(p, self.state)
    def on_created(self, event):
        self.on_modified(event)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--conv", type=str, default=None, help="Fichier .json à surveiller (optionnel).")
    parser.add_argument("--folder", type=str, default=None, help="Dossier conversations override (optionnel).")
    parser.add_argument("--poll", type=float, default=2.0, help="Intervalle de polling en secondes (si watchdog indisponible).")
    args = parser.parse_args()

    conv_folder = Path(args.folder) if args.folder else get_lmstudio_conversations_folder()
    if not conv_folder.exists():
        logging.error(f"Dossier conversations introuvable : {conv_folder}")
        return

    state = load_state()

    if args.conv:
        conv_file = Path(args.conv)
        if not conv_file.exists():
            logging.error(f"Fichier conversation introuvable: {conv_file}")
            return
        logging.info(f"Surveillance du fichier unique : {conv_file}")
        try:
            while True:
                process_updates_for_file(conv_file, state)
                time.sleep(args.poll)
        except KeyboardInterrupt:
            logging.info("Arrêt demandé (Ctrl+C). Sortie.")
            return

    logging.info(f"Surveillance du dossier conversations : {conv_folder} (poll={args.poll}s).")
    if WATCHDOG_AVAILABLE:
        observer = Observer()
        handler = SimpleWatchHandler(conv_folder, state)
        observer.schedule(handler, str(conv_folder), recursive=False)
        observer.start()
        try:
            # also process latest at start
            latest = max([p for p in conv_folder.glob("*.json")], key=lambda p: p.stat().st_mtime, default=None)
            if latest:
                process_updates_for_file(latest, state)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            observer.join()
            logging.info("Arrêt demandé (Ctrl+C). Sortie.")
            return
    else:
        # fallback polling loop
        current_file = None
        last_mtime = 0
        try:
            while True:
                jsons = [p for p in conv_folder.glob("*.json")]
                jsons = sorted(jsons, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
                latest = jsons[0] if jsons else None
                if latest is None:
                    if current_file is not None:
                        logging.info("Plus de fichiers JSON trouvés.")
                        current_file = None
                    time.sleep(args.poll)
                    continue
                try:
                    mtime = float(latest.stat().st_mtime)
                except Exception:
                    mtime = time.time()
                if current_file is None or latest.resolve() != current_file.resolve() or mtime != last_mtime:
                    current_file = latest
                    last_mtime = mtime
                    logging.info(f"Nouveau/fichier modifié surveillé : {current_file}")
                process_updates_for_file(current_file, state)
                time.sleep(args.poll)
        except KeyboardInterrupt:
            logging.info("Arrêt demandé (Ctrl+C). Sortie.")
            return

if __name__ == "__main__":
    main()
