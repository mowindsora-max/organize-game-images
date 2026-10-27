"""Scan images, verify exact duplicates, review by ID, plan and copy classifications."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif', '.heic', '.heif'}
KINDS = {'游戏', '真人照片', '原创或其他动漫', '风景物品', '截图文档', '其他', '待分类'}
STATES = {'pending', 'proposed', 'verified'}
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identical(a, b):
    with Path(a).open('rb') as left, Path(b).open('rb') as right:
        while True:
            aa, bb = left.read(1024 * 1024), right.read(1024 * 1024)
            if aa != bb:
                return False
            if not aa:
                return True


def csv_write(path, fields, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            safe = {key: ("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value)
                    for key, value in row.items()}
            writer.writerow(safe)


def scan(args):
    source, out = Path(args.source).resolve(strict=True), Path(args.out).resolve()
    if not source.is_dir() or inside(out, source):
        raise ValueError('Source must be a directory; output must be outside source')
    out.mkdir(parents=True, exist_ok=False)
    files, skipped, errors = [], [], []
    def walk_error(error):
        errors.append({'path': error.filename, 'error': str(error)})
    for root, dirs, names in os.walk(source, followlinks=False, onerror=walk_error):
        for name in list(dirs):
            path = Path(root) / name
            if path.is_symlink() or path.is_junction():
                dirs.remove(name)
                skipped.append({'path': str(path), 'reason': 'linked_directory'})
        for name in names:
            path = Path(root) / name
            if path.is_symlink():
                skipped.append({'path': str(path), 'reason': 'linked_file'})
            elif path.suffix.lower() in EXTENSIONS:
                files.append(path)
            else:
                skipped.append({'path': str(path), 'reason': 'unsupported_extension'})
    rows, candidates, duplicates = [], {}, []
    try:
        from PIL import Image
    except ImportError:
        Image = None
    for index, path in enumerate(sorted(files, key=lambda p: str(p).casefold()), 1):
        row = {'id': f'I{index:06}', 'path': str(path), 'relative_path': str(path.relative_to(source)),
               'filename': path.name, 'sha256': '', 'bytes': 0, 'width': '', 'height': '',
               'hint': '', 'duplicate_of': '', 'error': '', 'preview_error': ''}
        try:
            before = path.stat()
            row['sha256'], row['bytes'] = digest(path), before.st_size
            for previous in candidates.get(row['sha256'], []):
                if previous['bytes'] == row['bytes'] and identical(path, previous['path']):
                    row['duplicate_of'] = previous['id']
                    break
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError('File changed while scanning')
            if row['duplicate_of']:
                duplicates.append(row)
            else:
                candidates.setdefault(row['sha256'], []).append(row)
        except OSError as error:
            row['error'], row['duplicate_of'] = str(error), ''
        if Image is not None and not row['error']:
            try:
                with Image.open(path) as im:
                    row['width'], row['height'] = im.size
                    row['hint'] = ' | '.join(f'{key}: {str(im.info[key])[:1000]}'
                                            for key in ('parameters', 'Description', 'prompt') if key in im.info)[:2000]
                    im.verify()
            except Exception as error:
                row['preview_error'] = str(error)
        rows.append(row)
    summary = {'source': str(source), 'image_files': len(rows),
               'readable_files': sum(not r['error'] for r in rows),
               'representatives': sum(not r['duplicate_of'] and not r['error'] for r in rows),
               'duplicate_groups': len({r['duplicate_of'] for r in duplicates}),
               'duplicate_copies': len(duplicates), 'duplicate_bytes': sum(r['bytes'] for r in duplicates),
               'scan_errors': sum(bool(r['error']) for r in rows) + len(errors),
               'preview_errors': sum(bool(r['preview_error']) for r in rows),
               'skipped_entries': len(skipped), 'pillow_available': Image is not None}
    write_json(out / 'inventory.json', {'source': str(source), 'images': rows, 'walk_errors': errors, 'skipped': skipped})
    write_json(out / 'summary.json', summary)
    write_json(out / 'labels.json', [])
    csv_write(out / 'inventory.csv', list(rows[0]) if rows else ['id', 'path'], rows)
    by_id = {r['id']: r for r in rows}
    duplicate_rows = [dict(r, keep_path=by_id[r['duplicate_of']]['path']) for r in duplicates]
    csv_write(out / 'duplicates.csv', ['duplicate_of', 'keep_path', 'id', 'path', 'sha256', 'bytes'], duplicate_rows)
    print(json.dumps(summary, ensure_ascii=False))


def representatives(data):
    return [r for r in data['images'] if not r['duplicate_of'] and not r['error']]


def sheet(args):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    root = Path(args.scan)
    rows = representatives(read_json(root / 'inventory.json'))[args.offset:args.offset + args.limit]
    if not rows:
        raise ValueError('No images in requested range')
    font, tile_w, tile_h, columns = ImageFont.load_default(size=18), 280, 320, 4
    canvas = Image.new('RGB', (tile_w * columns, tile_h * ((len(rows) + columns - 1) // columns)), 'white')
    draw, index_rows = ImageDraw.Draw(canvas), []
    for n, row in enumerate(rows):
        x, y = (n % columns) * tile_w, (n // columns) * tile_h
        try:
            with Image.open(row['path']) as im:
                preview = ImageOps.exif_transpose(im)
                preview.thumbnail((tile_w - 12, tile_h - 50))
                preview = preview.convert('RGBA')
                canvas.paste(preview, (x + (tile_w - preview.width) // 2, y + 5), preview)
        except Exception as error:
            draw.text((x + 8, y + 50), 'Preview unavailable', font=font, fill='red')
            row = dict(row, preview_error=str(error))
        draw.text((x + 8, y + tile_h - 40), f"{row['id']}   {row['width']}x{row['height']}", font=font, fill='black')
        index_rows.append({key: row[key] for key in ('id', 'path', 'filename', 'hint', 'preview_error')})
    target = root / f'sheet-{args.offset:05}.jpg'
    canvas.save(target, quality=90)
    write_json(root / f'sheet-{args.offset:05}.json', index_rows)
    print(json.dumps({'sheet': str(target.resolve()), 'count': len(rows)}, ensure_ascii=False))


def component(value, max_length=60):
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError('Invalid directory or file component length')
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value in {'.', '..'} or value.endswith(('.', ' ')):
        raise ValueError(f'Invalid Windows component: {value!r}')
    if value.split('.')[0].upper() in RESERVED:
        raise ValueError(f'Reserved Windows component: {value!r}')
    return value


def plan(args):
    root = Path(args.scan)
    data = read_json(root / 'inventory.json')
    reps = {r['id']: r for r in representatives(data)}
    labels = {}
    for item in read_json(args.labels or root / 'labels.json'):
        identifier = item['id']
        if identifier not in reps or identifier in labels:
            raise ValueError(f'Unknown, duplicate or non-representative label ID: {identifier}')
        if item.get('status') not in STATES or item.get('kind') not in KINDS:
            raise ValueError(f'Invalid status or kind: {identifier}')
        if item['status'] == 'verified' and not item.get('evidence', '').strip():
            raise ValueError(f'Verified label requires evidence: {identifier}')
        for key in ('game', 'character'):
            if item.get(key):
                component(item[key])
        labels[identifier] = item
    plans, classes, targets = [], [], set()
    for row in data['images']:
        if row['error']:
            continue
        canonical = row['duplicate_of'] or row['id']
        label = labels.get(canonical, {'kind': '待分类', 'status': 'pending'})
        game, character = label.get('game') or '待识别游戏', label.get('character') or '待识别角色'
        if label['status'] == 'proposed':
            folder = Path('待核实')
        elif label['status'] == 'pending' or label['kind'] == '待分类':
            folder = Path('待分类')
        elif label['kind'] == '游戏':
            folder = Path('游戏') / component(game) / component(character)
        else:
            folder = Path(component(label['kind']))
        rep = reps[canonical]
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(rep['filename']).stem)[:65].rstrip('. ') or 'image'
        if stem.split('.')[0].upper() in RESERVED:
            stem = 'image_' + stem
        target = str(folder / f"{stem}__{rep['sha256'][:12]}{Path(rep['filename']).suffix.lower()}")
        classes.append(dict(row, kind=label['kind'], game=game if label['kind'] == '游戏' else '',
                            character=character if label['kind'] == '游戏' else '', status=label['status'],
                            evidence=label.get('evidence', ''), target=target))
        if not row['duplicate_of']:
            if target.casefold() in targets:
                raise ValueError('Target name collision; adjust classification or filename')
            targets.add(target.casefold())
            plans.append({'id': row['id'], 'path': row['path'], 'sha256': row['sha256'], 'target': target})
    write_json(root / 'copy-plan.json', {'source': data['source'], 'files': plans})
    csv_write(root / 'classification.csv', ['id', 'path', 'kind', 'game', 'character', 'status', 'evidence', 'duplicate_of', 'target'], classes)
    print(json.dumps({'representatives': len(plans), 'labeled_representatives': len(labels),
                      'verified': sum(l['status'] == 'verified' for l in labels.values()),
                      'proposed': sum(l['status'] == 'proposed' for l in labels.values())}, ensure_ascii=False))


def copy_files(args):
    root, destination = Path(args.scan), Path(args.dest).resolve()
    plan_data = read_json(root / 'copy-plan.json')
    source = Path(plan_data['source']).resolve(strict=True)
    if inside(destination, source) or inside(source, destination):
        raise ValueError('Source and destination must be separate, non-nested directories')
    prepared, used = [], set()
    for row in plan_data['files']:
        raw_source = Path(row['path'])
        original = raw_source.resolve(strict=True)
        relative, winpath = Path(row['target']), PureWindowsPath(row['target'])
        if raw_source.is_symlink() or not inside(original, source) or relative.is_absolute() or winpath.drive or winpath.root:
            raise ValueError('Plan source/target escapes its root or is linked')
        for part in relative.parts:
            component(part, max_length=200)
        target = (destination / relative).resolve()
        if not inside(target, destination) or target == destination or str(target).casefold() in used:
            raise ValueError('Plan target escapes destination or is duplicated')
        used.add(str(target).casefold())
        if digest(original) != row['sha256']:
            raise ValueError(f'Source changed since scan: {original}')
        prepared.append((row, original, target))
    destination.mkdir(parents=True, exist_ok=True)
    counts = {'copied': 0, 'skipped': 0, 'failed': 0}
    with (root / 'copy-log.jsonl').open('a', encoding='utf-8') as log:
        for row, original, target in prepared:
            created = False
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not inside(target, destination):
                    raise ValueError('Target escapes destination through linked parent')
                try:
                    output = target.open('xb')
                    created = True
                except FileExistsError:
                    if target.is_file() and digest(target) == row['sha256'] and identical(original, target):
                        counts['skipped'] += 1
                        log.write(json.dumps(dict(row, destination=str(target), result='skipped'), ensure_ascii=False) + '\n')
                        continue
                    raise ValueError('Destination exists with different content')
                with output, original.open('rb') as stream:
                    shutil.copyfileobj(stream, output)
                if digest(target) != row['sha256'] or not identical(original, target):
                    raise ValueError('Source changed or copied bytes differ')
                counts['copied'] += 1
                result = dict(row, destination=str(target), result='copied')
            except (OSError, ValueError) as error:
                if created:
                    target.unlink(missing_ok=True)
                counts['failed'] += 1
                result = dict(row, destination=str(target), result='failed', error=str(error))
            log.write(json.dumps(result, ensure_ascii=False) + '\n')
            log.flush()
    print(json.dumps(counts))
    return 1 if counts['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    command = subs.add_parser('scan')
    command.add_argument('--source', required=True)
    command.add_argument('--out', required=True)
    command.set_defaults(func=scan)
    command = subs.add_parser('sheet')
    command.add_argument('--scan', required=True)
    command.add_argument('--offset', type=int, default=0)
    command.add_argument('--limit', type=int, default=24)
    command.set_defaults(func=sheet)
    command = subs.add_parser('plan')
    command.add_argument('--scan', required=True)
    command.add_argument('--labels')
    command.set_defaults(func=plan)
    command = subs.add_parser('copy')
    command.add_argument('--scan', required=True)
    command.add_argument('--dest', required=True)
    command.set_defaults(func=copy_files)
    args = parser.parse_args()
    if args.command == 'sheet' and (args.offset < 0 or not 1 <= args.limit <= 48):
        parser.error('offset must be >= 0 and limit between 1 and 48')
    try:
        return args.func(args) or 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
