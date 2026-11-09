import json
from pathlib import Path
import socket
import subprocess
import sys
import os
import shutil
import shlex
import zipfile

import pytest

from cubercard import apkg, manifest as mf
from tests import apkg_fixture as fx


@pytest.mark.parametrize('name,size', [('clip.mkv', 5), ('picture.bmp', 5),
                                     ('empty.mp3', 0), ('large.mp3', 20 * 1024 * 1024 + 1)])
@pytest.mark.parametrize('fmt', ['legacy', 'new'])
def test_unusable_media_is_dropped_without_aborting_conversion(tmp_path, monkeypatch, name, size, fmt):
    data = b'x' * size
    media = f'<img src="{name}">' if name.endswith('.bmp') else f'[sound:{name}]'
    monkeypatch.setattr(fx, 'BASIC_FRONT', f'bonjour{media}')
    path = fx.build(tmp_path, fmt)
    # Keep the fixture collection/media index but replace the referenced sound file.
    with zipfile.ZipFile(path) as source:
        entries = {item.filename: source.read(item) for item in source.infolist()}
    if fmt == 'legacy':
        entries['media'] = json.dumps({'0': name, '1': 'map.png'}).encode()
        entries['0'] = data
    else:
        import zstandard
        compressor = zstandard.ZstdCompressor()
        index = b''.join(fx.pb_bytes(1, fx.pb_bytes(1, filename) + fx.pb_varint(2, len(body)))
                         for filename, body in ((name, data), ('map.png', fx.IMAGE)))
        entries['media'] = compressor.compress(index)
        entries['0'] = compressor.compress(data)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for member, body in entries.items():
            archive.writestr(member, body)
    output = tmp_path / 'out'
    report = apkg.convert(path, 'french', output)
    assert report['missing_media'] == [name]
    assert not (output / 'media' / name).exists()
    assert (output / 'media/map.png').is_file()
    assert len(mf.prepare(report['manifest'], output).cards) == 3
    assert name in '\n'.join(apkg.preview_lines(report))


def test_typed_answer_survives_on_back(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, 'BASIC_TMPL', ('Typed', '{{Front}}{{type:Back}}', '{{FrontSide}}<hr>{{type:Back}}'))
    report = apkg.convert(fx.build(tmp_path), 'french', tmp_path / 'out')
    card = next(c for c in report['manifest']['cards'] if c['external_key'] == f'anki:{fx.BASIC_NID}:0')
    assert any(b.get('text') == 'hello\na greeting' for b in card['back'])
    assert not any(b.get('text') == 'hello\na greeting' for b in card['front'])


def test_multiline_cloze_keeps_cards_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, 'CLOZE_TEXT', 'Before\n{{c1::first<br>second::two\nwords}} and {{c2::other}}')
    report = apkg.convert(fx.build(tmp_path), 'french', tmp_path / 'out')
    cards = report['manifest']['cards']
    assert len(cards) == 3
    c1 = next(c for c in cards if c['external_key'].endswith(':c1'))
    assert c1['term'] == 'first second'
    assert 'Before\n{{c1::first second::two words}}' in c1['front'][0]['text']
    assert len(mf.prepare(report['manifest'], tmp_path / 'out').cards) == 3


def test_cloze_front_respects_template_and_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, 'CLOZE_TMPL', ('Cloze', '{{Back Extra}}{{cloze:Text}}', '{{cloze:Text}}{{Back Extra}}'))
    path = fx.build(tmp_path)
    for mapping in ({}, {'note_types': {'Cloze': {'front': ['Text', 'Back Extra'], 'back': ['Text']}}}):
        report = apkg.convert(path, 'french', tmp_path / 'out', mapping, write=False)
        card = next(c for c in report['manifest']['cards'] if c['external_key'].endswith(':c1'))
        assert {'type': 'text', 'text': 'Europe'} in card['front']
        assert {'type': 'image', 'filename': 'media/map.png'} in card['front']
        assert not any(b['type'] == 'image' for b in card['back'])


def test_target_deck_scopes_default_batch_but_explicit_batch_stays_explicit(tmp_path):
    path = fx.build(tmp_path)
    def batch(deck, **kwargs):
        return apkg.convert(path, deck, tmp_path / 'out', write=False, **kwargs)['manifest']['batch_id']
    assert batch('one') == batch('one')
    assert batch('one') != batch('two')
    assert batch('one', batch_id='chosen') == 'chosen'


def test_target_deck_rejects_stale_tree_before_changing_output(tmp_path):
    path = fx.build(tmp_path)
    out = tmp_path / 'out'
    apkg.convert(path, None, out)
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()}
    with pytest.raises(apkg.ApkgError, match='decks.json'):
        apkg.convert(path, 'chosen', out)
    assert {p.relative_to(out): p.read_bytes() for p in out.rglob('*') if p.is_file()} == before


def test_e2e_occupied_port_never_terminates_its_owner(tmp_path, monkeypatch):
    # The port guard must work without lsof and never reach Wrangler/network.
    commands = tmp_path / 'bin'
    commands.mkdir()
    for name in ('dirname', 'mktemp', 'rm', 'mkdir', 'python3'):
        (commands / name).symlink_to(shutil.which(name))
    invoked = tmp_path / 'npx-invoked'
    npx = commands / 'npx'
    npx.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(invoked)) + '\nexit 99\n')
    npx.chmod(0o700)
    monkeypatch.setenv('PATH', str(commands))
    listener = subprocess.Popen([sys.executable, '-u', '-c',
        'import socket,time;s=socket.socket();s.bind(("127.0.0.1",0));s.listen();print(s.getsockname()[1],flush=True);time.sleep(60)'], stdout=subprocess.PIPE, text=True)
    try:
        assert listener.stdout is not None
        port = listener.stdout.readline().strip()
        result = subprocess.run(['/bin/bash', 'scripts/e2e.sh'], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'CUBERCARD_SERVER_DIR': str(tmp_path), 'E2E_PORT': port},
            text=True, capture_output=True, timeout=10)
        assert result.returncode != 0 and 'already in use' in result.stderr
        assert not invoked.exists(), 'unit test reached Wrangler instead of stopping at the port guard'
        assert listener.poll() is None, 'E2E cleanup killed a listener it did not start'
        with socket.create_connection(('127.0.0.1', int(port)), timeout=1):
            pass
    finally:
        listener.terminate()
        listener.wait(timeout=5)
