import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRANSCRIPT = """[00:00:05] Alice: Today we review the pump bracket for the P-200 mount.
[00:00:20] Bob: Option one is 6061-T6 aluminium at 3.2 mm. Option two is mild steel at 2 mm, cheaper but 40% heavier.
[00:01:02] Alice: Agreed, let's go with the aluminium at 3.2 mm because weight matters most.
[00:01:30] Bob: Who checks the fatigue life? Not sure yet.
[00:01:45] Alice: I'll run the FEA by Friday. Last time the steel bracket cracked at the weld."""


def item(**extra):
    base = {'text': 'x', 'quote': '', 'source_ids': [], 'basis': 'stated', 'uncertainty': None}
    base.update(extra)
    return base


def sample_payload():
    return {
        'context': [item(text='Pump bracket for the P-200 mount', quote='pump bracket for the P-200 mount',
                         source_ids=['S001'], aspect='subject')],
        'participants': [{'name': 'Alice', 'role': None, 'source_ids': ['S001']},
                         {'name': 'Bob', 'role': None, 'source_ids': ['S002']}],
        'decisions': [item(text='Use 6061-T6 aluminium at 3.2 mm', quote="let's go with the aluminium at 3.2 mm",
                           source_ids=['S003'], parameters=['6061-T6', '3.2 mm'], rationale='Weight matters most.')],
        'alternatives': [item(text='Mild steel at 2 mm', quote='mild steel at 2 mm', source_ids=['S002'],
                              option='Mild steel', advantages=['cheaper'], disadvantages=['40% heavier'],
                              outcome='rejected', outcome_reason='Weight matters most.')],
        'rationale': [item(text='Weight prioritized over cost', quote='because weight matters most',
                           source_ids=['S003'], kind='trade_off', factors=['weight', 'cost'])],
        'evidence': [],
        'open_issues': [item(text='Run FEA fatigue check', quote="I'll run the FEA by Friday", source_ids=['S005'],
                             kind='action', owner='Alice', due='Friday')],
        'lessons': [item(text='Previous steel bracket cracked at the weld', quote='the steel bracket cracked at the weld',
                         source_ids=['S005'])],
    }


class FakeClient:
    def __init__(self, payload=None, finish_reason='stop', segments=None, grounding=None):
        self.payload = sample_payload() if payload is None else payload
        self.grounding = grounding
        self.finish_reason = finish_reason
        self.requests = []
        self.transcriptions = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._transcribe))
        self.segments = segments if segments is not None else [
            {'start': 0.0, 'end': 4.0, 'text': ' We will use the 3.2 mm aluminium bracket.'},
            {'start': 4.0, 'end': 9.0, 'text': ' Agreed.'}]

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        payload = self.payload
        if kwargs.get('response_format', {}).get('json_schema', {}).get('name') == 'visual_grounding':
            payload = self.grounding(kwargs) if callable(self.grounding) else (self.grounding or {'results': []})
        content = payload if isinstance(payload, str) else json.dumps(payload)
        message = SimpleNamespace(content=content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=self.finish_reason)], usage=None)

    def _transcribe(self, **kwargs):
        self.transcriptions.append({k: v for k, v in kwargs.items() if k != 'file'})
        return SimpleNamespace(segments=self.segments, text=' '.join(s['text'] for s in self.segments))


@pytest.fixture
def fake_client():
    return FakeClient()
