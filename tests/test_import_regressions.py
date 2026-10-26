import json
from pathlib import Path
import socket
import subprocess
import sys
import os

from cubercard import apkg, manifest as mf
from tests import apkg_fixture as fx


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


def test_e2e_occupied_port_never_terminates_its_owner(tmp_path):
    listener = subprocess.Popen([sys.executable, '-u', '-c',
        'import socket,time;s=socket.socket();s.bind(("127.0.0.1",0));s.listen();print(s.getsockname()[1],flush=True);time.sleep(60)'], stdout=subprocess.PIPE, text=True)
    try:
        port = listener.stdout.readline().strip()
        result = subprocess.run(['bash', 'scripts/e2e.sh'], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'CUBERCARD_SERVER_DIR': str(tmp_path), 'E2E_PORT': port},
            text=True, capture_output=True, timeout=10)
        assert result.returncode != 0 and 'already in use' in result.stderr
        assert listener.poll() is None, 'E2E cleanup killed a listener it did not start'
        with socket.create_connection(('127.0.0.1', int(port)), timeout=1):
            pass
    finally:
        listener.terminate()
        listener.wait(timeout=5)
