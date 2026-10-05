import json
import os
import sys

import pytest

from recruit_agent.common import ROOT, DomainError, canonical, read_json
from recruit_agent.harnesses import HarnessAdapter, build_prompt, parse_business_json, parse_dsh, scoring_batch


@pytest.mark.parametrize("reason,exit_code",[("error",0),("completed",1),("aborted",1)])
def test_dsh_final_does_not_imply_success(reason,exit_code):
    stream="\n".join([canonical({"type":"status","phase":"turn_end","reason":reason}),canonical({"type":"final","text":'{"ok":true}'})])
    with pytest.raises(DomainError):parse_dsh(stream,exit_code)


def test_dsh_business_json_is_final_text_only():
    stream="\n".join([canonical({"type":"session","sessionId":"test"}),canonical({"type":"text","text":"intermediate"}),canonical({"type":"status","phase":"turn_end","reason":"completed"}),canonical({"type":"final","text":'{"ok":true}'})])
    result,events=parse_dsh(stream,0)
    assert result=={"ok":True} and len(events)==4


@pytest.mark.parametrize("text",['{"x":1}{"x":2}','[]','not json','```json\n{}\n``` extra'])
def test_ambiguous_output_is_not_repaired_by_guessing(text):
    with pytest.raises(DomainError):parse_business_json(text)


def test_model_input_excludes_identity_and_real_urls(job,batch):
    c=batch["candidates"][0]
    c["candidate_label"]="PRIVATE_REAL_NAME"
    c["actions"][0]["observed_platform_url"]="https://www.zhipin.com/?token=private"
    prompt=build_prompt(job,batch)
    assert '{{JOB_JSON}}' not in prompt and '{{BATCH_JSON}}' not in prompt
    assert "PRIVATE_REAL_NAME" not in prompt
    assert "token=private" not in prompt
    assert c["snapshot"]["scoring_text"] in prompt.replace('\\n','\n')


def fake_cli(tmp_path,kind,body=None):
    executable=tmp_path/("fake-"+kind)
    script = "#!"+sys.executable+"\nimport sys,json,os\n"
    script += "if '--version' in sys.argv:\n print('TEST_DOUBLE_ONLY');raise SystemExit(0)\n"
    if body:
        script += body
    else:
        script += f"sys.path.insert(0,{str(ROOT)!r})\n"
        script += "from recruit_agent.demo import fixture_evaluation\nprompt=sys.stdin.read()\n"
        script += "import re\nm=re.search(r'\\n\\nJOB_JSON\\n(.*?)\\n\\nBATCH_JSON\\n(.*?)\\n\\nEVALUATION_SCHEMA\\n',prompt,re.S)\n"
        script += "assert os.getenv('BOSS_COOKIE') is None\nresult=fixture_evaluation(json.loads(m[1]),json.loads(m[2]))\n"
        if kind=="dsh":
            script += "print(json.dumps({'type':'status','phase':'turn_end','reason':'completed'}))\nprint(json.dumps({'type':'final','text':json.dumps(result,ensure_ascii=False)},ensure_ascii=False))\n"
        else:
            script += "from pathlib import Path\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result,ensure_ascii=False))\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':111,'output_tokens':222}}))\n"
    executable.write_text(script,encoding="utf-8");executable.chmod(0o700)
    return str(executable)


@pytest.mark.parametrize("kind",["dsh","codex"])
def test_real_subprocess_adapter_contract_with_explicit_test_double(tmp_path,job,batch,monkeypatch,kind):
    config=read_json(ROOT/'config/default.json')["harness"]
    config.update(primary=kind,dsh_config_dir=str(tmp_path/'dsh-home'))
    config["executables"][kind]=fake_cli(tmp_path,kind)
    monkeypatch.setenv('BOSS_COOKIE','NEVER_INHERIT_THIS')
    output,metadata=HarnessAdapter(config).evaluate(job,batch)
    assert len(output["evaluations"])==10
    assert metadata["harness"]==kind
    assert metadata["input_mode"]=="synthetic"


def test_timeout_kills_subprocess_tree(tmp_path,job,batch):
    config=read_json(ROOT/'config/default.json')["harness"]
    config.update(primary='codex',timeout_seconds=.2)
    config["executables"]['codex']=fake_cli(tmp_path,'codex',"import time\nsys.stdin.read()\ntime.sleep(10)\n")
    with pytest.raises(DomainError) as caught:HarnessAdapter(config).evaluate(job,batch)
    assert caught.value.code=='HARNESS_TIMEOUT'


def test_real_resume_processing_requires_explicit_processor_configuration(tmp_path,job,batch):
    config=read_json(ROOT/'config/default.json')["harness"]
    config.update(primary='codex')
    config["executables"]['codex']=fake_cli(tmp_path,'codex')
    batch['mode']='manual'
    with pytest.raises(DomainError) as caught:HarnessAdapter(config).evaluate(job,batch)
    assert caught.value.code=='DATA_PROCESSOR_NOT_ENABLED'
