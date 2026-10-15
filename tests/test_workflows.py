import ast
import configparser
import importlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = yaml.safe_load((ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8"))
# PyYAML parses the bare key `on` as the boolean True
TRIGGERS = PUBLISH.get("on") or PUBLISH.get(True)


def steps():
    return PUBLISH["jobs"]["publish"]["steps"]


def command_index(script):
    for k, step in enumerate(steps()):
        if f"python {script}" in step.get("run", ""):
            return k
    raise AssertionError(f"no step runs {script}")


def test_pipeline_order_puts_tts_before_planning_and_images():
    order = [command_index(s) for s in ("generate_script.py", "synthesize_speech.py", "plan_shots.py",
                                         "generate_images.py", "ground_focus.py", "make_video.py",
                                         "upload_youtube.py", "publish_podcast.py")]
    assert order == sorted(order)


def test_job_timeout_allows_slow_image_generation():
    assert PUBLISH["jobs"]["publish"]["timeout-minutes"] == 120


def test_manual_run_inputs_exist_and_default_to_shots_and_cinematic():
    inputs = TRIGGERS["workflow_dispatch"]["inputs"]
    assert inputs["video_mode"]["default"] == "shots" and "slideshow" in inputs["video_mode"]["options"]
    assert inputs["visual_style"]["options"] == ["ink", "cinematic"] and inputs["visual_style"]["default"] == "cinematic"
    assert inputs["publish_podcast"]["type"] == "boolean" and inputs["publish_podcast"]["default"] is True
    env = PUBLISH["jobs"]["publish"]["env"]
    assert "inputs.video_mode || 'shots'" in env["VIDEO_MODE"]
    assert "inputs.visual_style || 'cinematic'" in env["VISUAL_STYLE"]
    assert env["MAX_IMAGES"] == "24"


@pytest.mark.parametrize("name_part", ["發布 Podcast", "Commit & Push"])
def test_podcast_and_rss_steps_can_be_skipped_on_manual_test_runs(name_part):
    step = next(s for s in steps() if name_part in s["name"])
    assert "inputs.publish_podcast" in step["if"] and "workflow_dispatch" in step["if"]


def test_review_artifacts_are_uploaded_even_when_the_run_fails():
    step = next(s for s in steps() if "review artifacts" in s["name"])
    assert step["if"] == "always()" and "contact_sheet.png" in step["with"]["path"]


def test_test_workflow_installs_ffmpeg_and_cjk_fonts_and_runs_pytest():
    text = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "ffmpeg" in text and "fonts-noto-cjk" in text and re.search(r"python -m pytest", text)
    assert "requirements-dev.txt" in text


# ---------------------------------------------------------------------------
# Wiring checks beyond the brief: failure modes that would otherwise first show up on a real Actions run.
# ---------------------------------------------------------------------------

SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
WORKFLOWS = ROOT / ".github" / "workflows"

PIPELINE = ["generate_script.py", "synthesize_speech.py", "plan_shots.py", "generate_images.py", "ground_focus.py",
            "make_video.py", "upload_youtube.py", "publish_podcast.py", "notify.py"]
REVIEW_FILES = ["contact_sheet.png", "video_report.json", "plan_report.json", "image_report.json",
                "ground_report.json", "shots.json", "timings.json", "subtitles.srt"]
DOCUMENTED_ENV = {"VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES", "IMAGE_THROTTLE_SEC", "IMAGE_STAGE_MAX_SEC", "IMAGE_UNIT_COST_USD",
                  "RENDER_WORKERS", "VIDEO_CRF", "SUBTITLE_FONT_FILE", "SUBTITLE_FONT_INDEX"}
PUBLISH_STEP_NAMES = [
    "Checkout", "Azure login (OIDC)", "Setup Python", "Install FFmpeg + 中文字型", "Install Python deps",
    "生成腳本 (Azure OpenAI GPT)", "TTS 語音合成 + 逐子句時間 (Azure Speech)", "規劃分鏡 (shots 模式)",
    "生成插畫 (gpt-image-2)", "聚焦定位 (shots 模式)", "影片合成", "上傳 YouTube",
    "發布 Podcast (Azure Blob + RSS)", "通知 Discord", "失敗時通知 (refresh token 過期等)",
    "Commit & Push 更新後的 RSS 與 log", "Upload review artifacts (縮圖總表與報告)", "Upload artifacts (debug)"]
PUBLISH_ENV_KEYS = {
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_GPT_DEPLOYMENT", "AZURE_OPENAI_IMAGE_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION", "AZURE_SPEECH_REGION", "AZURE_SPEECH_RESOURCE_ID", "AZURE_SPEECH_CUSTOM_DOMAIN",
    "AZURE_SPEECH_VOICE", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_CONTAINER", "AZURE_BLOB_PUBLIC_URL_BASE",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CHANNEL_ID", "YOUTUBE_PRIVACY",
    "PODCAST_TITLE", "PODCAST_AUTHOR", "PODCAST_EMAIL", "PODCAST_BASE_URL", "PODCAST_LANGUAGE", "PODCAST_CATEGORY",
    "DISCORD_WEBHOOK_URL", "VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES"}
PODCAST_STEP, NOTIFY_STEP, COMMIT_STEP = "發布 Podcast", "通知 Discord", "Commit & Push"
PODCAST_IF = "${{ github.event_name != 'workflow_dispatch' || inputs.publish_podcast }}"
BOTH_CHECKS_PASSED = "steps.check.outputs.skip == 'false' && steps.pr_check.outputs.skip == 'false'"
# (name, id, if, uses, working-directory, with minus the long PR body)
REPLENISH_STEPS = [
    ("Checkout (latest main with publish commit)", None, None, "actions/checkout@v5", None,
     {"fetch-depth": 0, "ref": "main"}),
    ("Setup Python", None, None, "actions/setup-python@v6", None, {"python-version": "3.11", "cache": "pip"}),
    ("Check remaining topics", "check", None, None, "scripts", None),
    ("Check existing replenish PR", "pr_check", "steps.check.outputs.skip == 'false'", None, None, None),
    ("Azure login (OIDC)", None, BOTH_CHECKS_PASSED, "azure/login@v3", None,
     {"client-id": "${{ secrets.AZURE_CLIENT_ID }}", "tenant-id": "${{ secrets.AZURE_TENANT_ID }}",
      "subscription-id": "${{ secrets.AZURE_SUBSCRIPTION_ID }}"}),
    ("Install Python deps", None, BOTH_CHECKS_PASSED, None, None, None),
    ("產生新題目 (Azure OpenAI)", None, BOTH_CHECKS_PASSED, None, "scripts", None),
    ("開 PR (peter-evans/create-pull-request)", None, BOTH_CHECKS_PASSED, "peter-evans/create-pull-request@v8", None,
     {"token": "${{ secrets.GITHUB_TOKEN }}", "branch": "replenish-topics/run-${{ github.run_id }}", "base": "main",
      "commit-message": "chore: replenish topics queue (+${{ env.REPLENISH_COUNT }} topics)",
      "title": "補充題材庫 +${{ env.REPLENISH_COUNT }} 題 (剩餘 ${{ steps.check.outputs.remaining }} 集)",
      "labels": "replenish-topics,automated"}),
    ("通知 Discord (skip)", None,
     "(steps.check.outputs.skip == 'true' || steps.pr_check.outputs.skip == 'true') "
     "&& env.DISCORD_WEBHOOK_URL != ''", None, None, None),
    ("通知 Discord (PR 已開)", None, f"{BOTH_CHECKS_PASSED} && env.DISCORD_WEBHOOK_URL != ''", None, None, None)]
PUBLISH_PERMISSIONS = {"contents": "write", "id-token": "write", "pull-requests": "write"}
PUBLISH_USES = [
    ("Checkout", "actions/checkout@v5"), ("Azure login (OIDC)", "azure/login@v3"),
    ("Setup Python", "actions/setup-python@v6"),
    ("Upload review artifacts (縮圖總表與報告)", "actions/upload-artifact@v5"),
    ("Upload artifacts (debug)", "actions/upload-artifact@v5")]
RUN_SUFFIX = "${{ github.run_id }}-${{ github.run_attempt }}"   # re-running a run keeps run_id; artifact names are immutable
TEST_WORKFLOW_STEPS = [
    {"name": "Checkout", "uses": "actions/checkout@v5"},
    {"name": "Setup Python", "uses": "actions/setup-python@v6", "with": {"python-version": "3.11", "cache": "pip"}},
    {"name": "Install FFmpeg + 中文字型",
     "run": "sudo apt-get update -qq\nsudo apt-get install -y -qq ffmpeg fonts-noto-cjk\n"},
    {"name": "Install Python deps", "run": "pip install -r requirements.txt -r requirements-dev.txt"},
    {"name": "Run tests", "run": "python -m pytest -q"}]

# third-party import root -> acceptable requirement names (google-auth is pulled in by each of the google packages)
GOOGLE_AUTH = {"google-auth-oauthlib", "google-auth-httplib2", "google-api-python-client"}
DIST_FOR_MODULE = {
    "PIL": {"pillow"}, "numpy": {"numpy"}, "requests": {"requests"}, "openai": {"openai"}, "yaml": {"pyyaml"},
    "dotenv": {"python-dotenv"}, "feedgen": {"feedgen"}, "mutagen": {"mutagen"}, "pytest": {"pytest"},
    "imageio_ffmpeg": {"imageio-ffmpeg"}, "azure.identity": {"azure-identity"},
    "azure.cognitiveservices": {"azure-cognitiveservices-speech"}, "azure.storage": {"azure-storage-blob"},
    "azure.core": {"azure-core"}, "googleapiclient": {"google-api-python-client"},
    "google_auth_oauthlib": {"google-auth-oauthlib"}, "google.oauth2": GOOGLE_AUTH, "google.auth": GOOGLE_AUTH}
LOCAL_MODULES = {p.stem for folder in (SCRIPTS, TESTS) for p in folder.glob("*.py")}


def read(path):
    return path.read_text(encoding="utf-8")


def parse(path):
    return ast.parse(read(path))


def load_test_workflow():
    return yaml.safe_load(read(WORKFLOWS / "test.yml"))


def named_step(part, job_steps=None):
    return next(s for s in (steps() if job_steps is None else job_steps) if part in s["name"])


def run_script(step):
    match = re.fullmatch(r"python (\w+\.py)", step.get("run", "").strip())
    return match.group(1) if match else None


def pipeline_steps():
    return [(run_script(s), s) for s in steps() if run_script(s)]


class _Context:
    def __init__(self, **values):
        self._values = values

    def __getattr__(self, name):
        return self._values.get(name)


def evaluate(expression, event, secrets=None, **inputs):
    """Evaluates the small expression subset these workflows use (||, !=, string literals, inputs.*, secrets.*)."""
    match = re.fullmatch(r"\$\{\{\s*(.+?)\s*\}\}", expression.strip())
    assert match, f"not a ${{{{ }}}} expression: {expression!r}"
    python = match.group(1).replace("||", " or ")
    assert re.fullmatch(r"[\w\s.'!=-]+", python) and "__" not in python, f"unsupported syntax: {expression!r}"
    scope = {"github": _Context(event_name=event), "inputs": _Context(**inputs), "secrets": _Context(**(secrets or {}))}
    return eval(python, {"__builtins__": {}}, scope)  # nosec: expression text comes from this repo's own workflow


def main_guard(tree):
    for node in tree.body:
        test = node.test if isinstance(node, ast.If) else None
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
                and [c.value for c in test.comparators if isinstance(c, ast.Constant)] == ["__main__"]):
            return node
    return None


def _catches_import_error(handler):
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError") for n in names)


def import_records(tree):
    """(dotted module, guarded by try/except ImportError) for every absolute import in the tree."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    records = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        else:
            continue
        guarded, child = False, node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.Try) and child in parent.body and any(
                    _catches_import_error(h) for h in parent.handlers):
                guarded = True
            child = parent
        records.extend((module, guarded) for module in modules)
    return records


def third_party_imports(path):
    skip = sys.stdlib_module_names | LOCAL_MODULES
    return sorted({(m, g) for m, g in import_records(parse(path)) if m.split(".")[0] not in skip})


def requirement_names(path):
    names = set()
    for line in read(path).splitlines():
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line.split("#")[0].strip())
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group()).lower())
    return names


def dists_for(module):
    for prefix in sorted(DIST_FOR_MODULE, key=len, reverse=True):
        if module == prefix or module.startswith(prefix + "."):
            return DIST_FOR_MODULE[prefix]
    return None


def _is_os_environ(node):
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def env_reads(tree):
    """(name, required) for env("X") / os.environ.get("X") / os.environ["X"]."""
    reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            name, func = node.args[0].value, node.func
            if isinstance(func, ast.Name) and func.id == "env":
                has_default = len(node.args) > 1 or any(k.arg == "default" for k in node.keywords)
                optional = any(k.arg == "required" and isinstance(k.value, ast.Constant) and k.value.value is False
                               for k in node.keywords)
                reads.append((name, not (has_default or optional)))
            elif isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
                reads.append((name, False))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value) \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            reads.append((node.slice.value, True))
    return reads


def env_names_read_by_scripts():
    return {name for path in SCRIPTS.glob("*.py") for name, _ in env_reads(parse(path))}


def dict_keys_written_by(path):
    return {k.value for n in ast.walk(parse(path)) if isinstance(n, ast.Dict) for k in n.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def script_string_literals():
    return {n.value for path in SCRIPTS.glob("*.py") for n in ast.walk(parse(path))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def script_closure(roots):
    seen, todo = set(), [Path(r).stem for r in roots]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            tops = {m.split(".")[0] for m, _ in import_records(parse(SCRIPTS / f"{name}.py"))}
            todo.extend(t for t in tops if (SCRIPTS / f"{t}.py").is_file())
    return seen


# --- the pipeline's script wiring -------------------------------------------------------------------------------

def test_publish_job_runs_exactly_the_pipeline_scripts_once_and_in_order():
    # build_video.py must stay reachable only through make_video.py: a leftover direct step would build twice
    assert [name for name, _ in pipeline_steps()] == PIPELINE


def test_publish_job_steps_are_the_expected_ones_in_the_expected_order():
    assert [s["name"] for s in steps()] == PUBLISH_STEP_NAMES


@pytest.mark.parametrize("script", PIPELINE)
def test_pipeline_step_runs_an_existing_script_from_the_scripts_directory(script):
    step = next(s for name, s in pipeline_steps() if name == script)
    assert step.get("working-directory") == "scripts"
    assert (SCRIPTS / script).is_file()


@pytest.mark.parametrize("script", [s for s in PIPELINE if s != "generate_script.py"])
def test_pipeline_scripts_take_the_episode_id_from_env_or_argv_like_their_siblings(script):
    tree = parse(SCRIPTS / script)
    guard = main_guard(tree)
    assert guard is not None, f"{script} has no __main__ entry point"
    assert "EPISODE_ID" in {n.value for n in ast.walk(guard) if isinstance(n, ast.Constant)}
    assert any(isinstance(n, ast.Attribute) and n.attr == "argv" for n in ast.walk(guard))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert [a.arg for a in main.args.args][:1] == ["episode_id"]


def test_generate_script_hands_the_episode_id_to_the_later_steps(monkeypatch, tmp_path):
    import common
    import generate_script
    github_env = tmp_path / "github_env"
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setattr(generate_script, "pick_next_topic", lambda: {"id": 7, "title": "t"})
    monkeypatch.setattr(generate_script, "generate", lambda topic: {"title": "t", "sections": []})
    assert main_guard(parse(SCRIPTS / "generate_script.py")) is not None
    assert generate_script.main() == 0
    assert github_env.read_text(encoding="utf-8") == "EPISODE_ID=7\n"
    assert "EPISODE_ID" not in PUBLISH["jobs"]["publish"]["env"]
    assert "EPISODE_ID" in named_step(COMMIT_STEP)["run"]


def test_every_required_env_var_of_the_pipeline_scripts_is_provided_by_the_workflow():
    provided = set(PUBLISH["jobs"]["publish"]["env"]) | {"EPISODE_ID"}
    required = {name for module in script_closure(PIPELINE)
                for name, needed in env_reads(parse(SCRIPTS / f"{module}.py")) if needed}
    assert required <= provided, f"scripts require env vars the workflow does not set: {sorted(required - provided)}"


# --- the slideshow path (scheduled runs, and manual runs left on the default) is unchanged in meaning ----------------

@pytest.fixture(params=[None, "slideshow", ""], ids=["unset", "slideshow", "empty"])
def slideshow_run(request, monkeypatch, tmp_path):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    if request.param is None:
        monkeypatch.delenv("VIDEO_MODE", raising=False)
    else:
        monkeypatch.setenv("VIDEO_MODE", request.param)
    return tmp_path


def _unexpected(*args, **kwargs):
    raise AssertionError("slideshow mode must not reach this")


def test_planning_and_grounding_are_no_ops_in_slideshow_mode(slideshow_run, monkeypatch):
    import ground_focus
    import plan_shots
    monkeypatch.setattr(plan_shots, "make_client", _unexpected)
    monkeypatch.setattr(ground_focus, "make_client", _unexpected)
    assert plan_shots.main(321) == 0
    assert ground_focus.main(321) == 0
    assert list(slideshow_run.iterdir()) == []


@pytest.mark.parametrize("module_name", ["plan_shots", "ground_focus"])
def test_shots_mode_is_what_switches_planning_and_grounding_on(module_name, monkeypatch, tmp_path):
    import common
    module = importlib.import_module(module_name)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    monkeypatch.setattr(module, "make_client", _unexpected)
    with pytest.raises(OSError):   # reached the real work: the empty episode folder has no script.json / shots.json
        module.main(321)


def test_image_generation_takes_the_slideshow_path_by_default(slideshow_run, monkeypatch):
    import generate_images
    client, calls = object(), []
    monkeypatch.delenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", raising=False)
    monkeypatch.setattr(generate_images, "make_client", lambda timeout=300.0: client)
    monkeypatch.setattr(generate_images, "main_slideshow", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(generate_images, "load_json", _unexpected)
    assert generate_images.main(77) == 0
    assert calls == [(77, client, "gpt-image-2")]
    assert list(slideshow_run.iterdir()) == []


def test_video_composition_goes_straight_to_the_slideshow_builder_by_default(slideshow_run, monkeypatch):
    import make_video
    calls = []
    monkeypatch.setattr(make_video.build_video, "main", lambda episode_id: calls.append(episode_id) or 17)
    monkeypatch.setattr(make_video.render_video, "main", _unexpected)
    assert make_video.main(88) == 17
    assert calls == [88]
    assert list(slideshow_run.iterdir()) == []


# --- trigger inputs, job environment, and the untouched replenish job -------------------------------------------------

def test_scheduled_runs_use_shots_and_cinematic_by_default():
    env = PUBLISH["jobs"]["publish"]["env"]
    assert TRIGGERS["schedule"] == [{"cron": "0 21 * * *"}]
    assert evaluate(env["VIDEO_MODE"], "schedule") == "shots"
    assert evaluate(env["VISUAL_STYLE"], "schedule") == "cinematic"
    # a manual run left on its defaults behaves like the schedule; choosing the old slideshow / ink still works
    inputs = TRIGGERS["workflow_dispatch"]["inputs"]
    assert evaluate(env["VIDEO_MODE"], "workflow_dispatch", video_mode=inputs["video_mode"]["default"]) == "shots"
    assert evaluate(env["VISUAL_STYLE"], "workflow_dispatch", visual_style=inputs["visual_style"]["default"]) == "cinematic"
    assert evaluate(env["VIDEO_MODE"], "workflow_dispatch", video_mode="slideshow") == "slideshow"
    assert evaluate(env["VISUAL_STYLE"], "workflow_dispatch", visual_style="ink") == "ink"
    assert evaluate(env["YOUTUBE_PRIVACY"], "schedule") == "public"


def test_manual_choices_reach_the_job_environment():
    env = PUBLISH["jobs"]["publish"]["env"]
    manual = dict(video_mode="shots", visual_style="cinematic", privacy="private")
    assert evaluate(env["VIDEO_MODE"], "workflow_dispatch", **manual) == "shots"
    assert evaluate(env["VISUAL_STYLE"], "workflow_dispatch", **manual) == "cinematic"
    assert evaluate(env["YOUTUBE_PRIVACY"], "workflow_dispatch", **manual) == "private"


def test_manual_run_input_options_match_what_the_code_accepts():
    import visual_style
    inputs = TRIGGERS["workflow_dispatch"]["inputs"]
    assert inputs["video_mode"]["options"] == ["slideshow", "shots"] and inputs["video_mode"]["type"] == "choice"
    assert set(inputs["visual_style"]["options"]) == set(visual_style.STYLE_SUFFIXES)
    assert inputs["visual_style"]["default"] == visual_style.DEFAULT_STYLE
    assert inputs["privacy"]["default"] == "unlisted"
    assert inputs["privacy"]["options"] == ["public", "unlisted", "private"]


def test_publish_job_env_is_exactly_the_expected_set_of_variables():
    assert set(PUBLISH["jobs"]["publish"]["env"]) == PUBLISH_ENV_KEYS


DEFAULTED_SECRETS = {"AZURE_OPENAI_API_VERSION": "2024-10-21"}


def test_secret_backed_env_vars_read_the_secret_of_the_same_name():
    env = PUBLISH["jobs"]["publish"]["env"]
    secret_backed = {k: v for k, v in env.items() if "secrets." in v}
    assert set(env) - set(secret_backed) == {"YOUTUBE_PRIVACY", "VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES"}
    assert secret_backed == {k: "${{ secrets." + k + (f" || '{DEFAULTED_SECRETS[k]}'" if k in DEFAULTED_SECRETS else "")
                             + " }}" for k in secret_backed}


@pytest.mark.parametrize("secret, expected", [(None, "2024-10-21"), ("", "2024-10-21"),
                                              ("2025-04-01-preview", "2025-04-01-preview")])
@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_an_unset_api_version_secret_keeps_the_azure_route_with_the_previous_default(event, secret, expected):
    # llm_client.make_client switches to the v1 route when AZURE_OPENAI_API_VERSION is empty: the daily run must not
    value = PUBLISH["jobs"]["publish"]["env"]["AZURE_OPENAI_API_VERSION"]
    assert "secrets.AZURE_OPENAI_API_VERSION" in value and "'2024-10-21'" in value
    assert evaluate(value, event, secrets={} if secret is None else {"AZURE_OPENAI_API_VERSION": secret}) == expected


def test_the_api_version_default_is_the_one_the_other_azure_clients_use_and_replenish_is_untouched():
    assert DEFAULTED_SECRETS["AZURE_OPENAI_API_VERSION"] == "2024-10-21"
    assert 'env("AZURE_OPENAI_API_VERSION", "2024-10-21")' in read(SCRIPTS / "replenish_topics.py")
    assert PUBLISH["jobs"]["replenish"]["env"]["AZURE_OPENAI_API_VERSION"] == "${{ secrets.AZURE_OPENAI_API_VERSION }}"


def test_no_publish_step_overrides_the_mode_or_style_variables():
    for step in steps():
        assert not {"VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES"} & set(step.get("env", {})), step["name"]


def test_max_images_in_the_workflow_matches_the_code_default(monkeypatch):
    import plan_shots
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    assert PUBLISH["jobs"]["publish"]["env"]["MAX_IMAGES"] == str(plan_shots.max_images())


def test_replenish_job_is_untouched():
    job = PUBLISH["jobs"]["replenish"]
    assert (job["needs"], job["if"], job["runs-on"], job["timeout-minutes"]) == ("publish", "success()",
                                                                                  "ubuntu-latest", 15)
    assert job["env"]["LOW_THRESHOLD"] == "21" and job["env"]["REPLENISH_COUNT"] == "100"
    assert set(job["env"]) == {"AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_GPT_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
                               "DISCORD_WEBHOOK_URL", "LOW_THRESHOLD", "REPLENISH_COUNT"}
    summary = [(s["name"], s.get("id"), " ".join(s["if"].split()) if "if" in s else None, s.get("uses"),
                s.get("working-directory"),
                {k: v for k, v in s["with"].items() if k != "body"} if "with" in s else None) for s in job["steps"]]
    assert summary == REPLENISH_STEPS
    pr_body = next(s["with"]["body"] for s in job["steps"] if s.get("uses", "").startswith("peter-evans/"))
    assert all(ref in pr_body for ref in ("steps.check.outputs.remaining", "env.REPLENISH_COUNT", "env.LOW_THRESHOLD"))
    commands = [s["run"].strip() for s in job["steps"] if s.get("run", "").strip().startswith(("pip", "python"))]
    assert commands == ["pip install -r requirements.txt", 'python replenish_topics.py --count "$REPLENISH_COUNT"']


# --- step conditions and the steps around notification / commit / artifacts -------------------------------------------

def test_only_the_intended_publish_steps_are_conditional():
    conditional = {s["name"] for s in steps() if "if" in s}
    assert conditional == {"發布 Podcast (Azure Blob + RSS)", "通知 Discord", "失敗時通知 (refresh token 過期等)",
                           "Commit & Push 更新後的 RSS 與 log", "Upload review artifacts (縮圖總表與報告)",
                           "Upload artifacts (debug)"}


@pytest.mark.parametrize("name_part", [PODCAST_STEP, COMMIT_STEP])
def test_podcast_and_commit_steps_use_the_exact_skip_expression(name_part):
    assert named_step(name_part)["if"] == PODCAST_IF


@pytest.mark.parametrize("event, inputs, expected", [
    ("schedule", {}, True),
    ("workflow_dispatch", {"publish_podcast": True}, True),
    ("workflow_dispatch", {"publish_podcast": False}, False)])
@pytest.mark.parametrize("name_part", [PODCAST_STEP, COMMIT_STEP])
def test_podcast_and_commit_run_on_schedule_and_when_ticked_but_not_when_unticked(name_part, event, inputs, expected):
    assert bool(evaluate(named_step(name_part)["if"], event, **inputs)) is expected


def test_notification_runs_after_the_podcast_step_and_before_the_commit():
    names = [s["name"] for s in steps()]
    notify = named_step(NOTIFY_STEP)
    assert names.index(named_step(PODCAST_STEP)["name"]) < names.index(notify["name"]) \
        < names.index(named_step(COMMIT_STEP)["name"])
    assert notify == {"name": "通知 Discord", "if": "always()", "working-directory": "scripts",
                      "env": {"PUBLISH_PODCAST": PODCAST_IF}, "run": "python notify.py", "continue-on-error": True}


def render_boolean(value):
    """GitHub renders a boolean expression result into an environment variable as `true` / `false`."""
    return str(bool(value)).lower()


@pytest.mark.parametrize("event, inputs, expected", [
    ("schedule", {}, "true"),
    ("workflow_dispatch", {"publish_podcast": True}, "true"),
    ("workflow_dispatch", {"publish_podcast": False}, "false")])
def test_notification_is_told_whether_the_podcast_was_published(event, inputs, expected, monkeypatch):
    import notify
    value = render_boolean(evaluate(named_step(NOTIFY_STEP)["env"]["PUBLISH_PODCAST"], event, **inputs))
    assert value == expected
    monkeypatch.setenv("PUBLISH_PODCAST", value)
    podcast_line = notify.build_message("t", "u", "").splitlines()[-1]
    assert podcast_line == (" Podcast: 已更新 RSS feed" if expected == "true" else " Podcast: 未發布 (測試執行)")


def test_the_variable_the_notification_step_sets_is_the_one_notify_reads():
    assert "PUBLISH_PODCAST" in env_names_read_by_scripts()
    assert not [s["name"] for s in steps() if "PUBLISH_PODCAST" in s.get("env", {}) and s["name"] != NOTIFY_STEP]
    assert "PUBLISH_PODCAST" not in PUBLISH["jobs"]["publish"]["env"]


def test_failure_notification_step_is_intact():
    step = named_step("失敗時通知")
    assert step["if"] == "failure() && env.DISCORD_WEBHOOK_URL != ''"
    for needle in ("curl", '"$DISCORD_WEBHOOK_URL"', "YOUTUBE_REFRESH_TOKEN", "get_youtube_refresh_token.py",
                   "${{ github.run_id }}"):
        assert needle in step["run"]


def test_commit_step_still_commits_the_rss_and_the_publish_log():
    run = named_step(COMMIT_STEP)["run"]
    for line in ("git pull", "git add podcast.xml data/published_log.json", "git push",
                 'git commit -m "Publish episode ${EPISODE_ID:-unknown}"'):
        assert line in run


def test_review_artifact_step_uploads_every_report_and_runs_after_the_commit():
    names = [s["name"] for s in steps()]
    step = named_step("review artifacts")
    assert names.index(step["name"]) > names.index(named_step(COMMIT_STEP)["name"])
    assert step["uses"] == named_step("debug")["uses"] == "actions/upload-artifact@v5"
    assert step["with"]["name"] == f"episode-review-{RUN_SUFFIX}"
    assert set(step["with"]) == {"name", "path", "if-no-files-found", "retention-days"}
    assert step["with"]["path"].split() == [f"output/**/{name}" for name in REVIEW_FILES]
    assert step["with"]["if-no-files-found"] == "ignore" and step["with"]["retention-days"] == 14


def test_artifact_names_are_unique_per_run_attempt():
    uploads = [s for s in steps() if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 2

    def rendered(name, attempt):
        return name.replace("${{ github.run_id }}", "7").replace("${{ github.run_attempt }}", str(attempt))

    for step in uploads:   # a re-run keeps run_id, and upload-artifact v4+ rejects a second artifact of the same name
        assert rendered(step["with"]["name"], 1) != rendered(step["with"]["name"], 2), step["name"]


def test_review_artifact_file_names_are_the_ones_the_scripts_write():
    assert set(REVIEW_FILES) <= script_string_literals()


def test_debug_artifact_step_is_unchanged_apart_from_the_attempt_suffix():
    assert named_step("debug") == {
        "name": "Upload artifacts (debug)", "if": "failure()", "uses": "actions/upload-artifact@v5",
        "with": {"name": f"episode-debug-{RUN_SUFFIX}", "path": "output/\n!output/**/*.mp4\n", "retention-days": 7}}


def test_publish_job_pins_the_workflow_permissions_and_every_action_version():
    assert PUBLISH["permissions"] == PUBLISH_PERMISSIONS
    assert "permissions" not in PUBLISH["jobs"]["publish"]
    assert PUBLISH["jobs"]["replenish"]["permissions"] == PUBLISH_PERMISSIONS
    assert [(s["name"], s["uses"]) for s in steps() if "uses" in s] == PUBLISH_USES
    assert named_step("Checkout")["with"] == {"fetch-depth": 0}
    assert named_step("Azure login")["with"] == {
        "client-id": "${{ secrets.AZURE_CLIENT_ID }}", "tenant-id": "${{ secrets.AZURE_TENANT_ID }}",
        "subscription-id": "${{ secrets.AZURE_SUBSCRIPTION_ID }}"}
    assert named_step("Setup Python")["with"] == {"python-version": "3.11", "cache": "pip"}


def test_ffmpeg_and_dependency_install_steps_are_unchanged():
    ffmpeg = named_step("Install FFmpeg")["run"]
    assert "apt-get install -y -qq ffmpeg fonts-noto-cjk" in ffmpeg and "grep -q libx264" in ffmpeg
    assert named_step("Install Python deps")["run"] == "pip install -r requirements.txt"


# --- test.yml -----------------------------------------------------------------------------------------------------------

def apt_packages(run_text):
    packages = set()
    for line in run_text.splitlines():
        if "apt-get install" in line:
            packages |= {t for t in line.split("apt-get install", 1)[1].split() if not t.startswith("-")}
    return packages


def test_test_workflow_reuses_the_publish_workflows_action_pins_and_python():
    test_steps = load_test_workflow()["jobs"]["test"]["steps"]
    for action in ("actions/checkout", "actions/setup-python"):
        wanted = next(s for s in steps() if s.get("uses", "").startswith(action + "@"))
        got = next(s for s in test_steps if s.get("uses", "").startswith(action + "@"))
        assert got["uses"] == wanted["uses"]
        assert got.get("with", {}).get("python-version") == wanted.get("with", {}).get("python-version")
    test_python = next(s for s in test_steps if "setup-python" in s.get("uses", ""))
    assert test_python["with"] == named_step("Setup Python")["with"]
    assert load_test_workflow()["jobs"]["test"]["runs-on"] == PUBLISH["jobs"]["publish"]["runs-on"]


def test_test_workflow_installs_the_same_system_packages_as_publish():
    test_run = named_step("Install FFmpeg", load_test_workflow()["jobs"]["test"]["steps"])["run"]
    publish_run = named_step("Install FFmpeg")["run"]
    assert apt_packages(test_run) == apt_packages(publish_run) >= {"ffmpeg", "fonts-noto-cjk"}
    assert test_run.index("apt-get update") < test_run.index("apt-get install")


def test_test_workflow_installs_requirements_then_runs_pytest():
    runs = [s["run"].strip() for s in load_test_workflow()["jobs"]["test"]["steps"] if "run" in s]
    assert "apt-get install" in runs[0]
    assert runs[1:] == ["pip install -r requirements.txt -r requirements-dev.txt", "python -m pytest -q"]


def test_test_workflow_steps_are_exactly_these_with_no_extra_keys():
    workflow = load_test_workflow()
    assert set(workflow) == {"name", "on", "permissions", "jobs"} or set(workflow) == {"name", True, "permissions", "jobs"}
    job = workflow["jobs"]["test"]
    assert set(workflow["jobs"]) == {"test"} and set(job) == {"runs-on", "timeout-minutes", "steps"}
    assert job["steps"] == TEST_WORKFLOW_STEPS   # a continue-on-error / if / working-directory here would hide failures


def test_test_workflow_is_read_only_offline_and_triggered_by_code_changes():
    text = read(WORKFLOWS / "test.yml")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on") or workflow.get(True)
    assert set(triggers) == {"push", "pull_request"}
    assert triggers["push"]["paths"] == triggers["pull_request"]["paths"]
    assert {"scripts/**", "tests/**", "templates/**", "requirements*.txt", "pytest.ini",
            ".github/workflows/test.yml"} <= set(triggers["push"]["paths"])
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["test"]["timeout-minutes"] == 20
    assert "secrets." not in text and "azure/login" not in text


def github_path_pattern(glob):
    """Compiles a workflow `paths:` glob: `*` stays inside one path segment, `**` crosses `/`, `?` is one char."""
    regex, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif glob[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif glob[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(glob[i]), i + 1
    return re.compile(regex)


def path_matches(path, globs):
    return any(github_path_pattern(glob).fullmatch(path) for glob in globs)


@pytest.mark.parametrize("glob, path, expected", [
    ("scripts/**", "scripts/a.py", True), ("scripts/**", "scripts/sub/dir/a.py", True),
    ("scripts/**", "scriptsx/a.py", False), ("scripts/**", "tests/scripts/a.py", False),
    ("requirements*.txt", "requirements.txt", True), ("requirements*.txt", "requirements-dev.txt", True),
    ("requirements*.txt", "docs/requirements.txt", False), ("requirements*.txt", "requirements.txt.bak", False),
    ("*.md", "README.md", True), ("*.md", "docs/MANUAL_SETUP.md", False),
    ("a?c", "abc", True), ("a?c", "a/c", False), ("a?c", "ac", False),
    ("README.md", "README.md", True), ("README.md", "docs/README.md", False), ("README.md", "READMEXmd", False),
    (".env.example", ".env.example", True), (".env.example", "Xenv.example", False),
    ("docs/MANUAL_SETUP.md", "docs/MANUAL_SETUP.md", True), ("docs/MANUAL_SETUP.md", "docs/OTHER.md", False)])
def test_path_filter_matcher_follows_the_github_glob_rules(glob, path, expected):
    assert path_matches(path, [glob]) is expected


def repo_files_read_by(source):
    """Repo-relative files a module's source reads: `ROOT / "a" / "b"` chains (also through module-level aliases such as
    SCRIPTS), `<dir>.glob(...)` expansions, and string constants naming an existing file at the repo root (e.g. a
    parametrized "README.md")."""
    tree = ast.parse(source)
    aliases = {}

    def parts_of(node):
        if isinstance(node, ast.Name):
            return [] if node.id == "ROOT" else aliases.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) \
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            left = parts_of(node.left)
            return None if left is None else left + node.right.value.split("/")
        return None

    not_reads = set()   # sub-expressions of a longer path, alias definitions and glob receivers are bases, not reads
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            parts = parts_of(node.value)
            if parts:
                aliases[node.targets[0].id] = parts
                not_reads.add(id(node.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            not_reads.add(id(node.left))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("glob", "rglob"):
            not_reads.add(id(node.func.value))
    found = set()
    for node in ast.walk(tree):
        parts = parts_of(node) if isinstance(node, ast.BinOp) and id(node) not in not_reads else None
        if parts and not ROOT.joinpath(*parts).is_dir():
            found.add("/".join(parts))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("glob", "rglob") \
                and node.args and isinstance(node.args[0], ast.Constant) and parts_of(node.func.value) is not None:
            base = ROOT.joinpath(*parts_of(node.func.value))
            found |= {p.relative_to(ROOT).as_posix() for p in getattr(base, node.func.attr)(node.args[0].value)
                      if p.is_file()}
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and 0 < len(node.value) <= 100 \
                and "\n" not in node.value:
            try:
                if (ROOT / node.value).is_file():
                    found.add(node.value)
            except (OSError, ValueError):
                pass
    return found


def repo_files_read_by_this_module():
    return repo_files_read_by(read(Path(__file__)))


@pytest.mark.parametrize("source, expected", [
    ('read(ROOT / "docs" / "x.md")', {"docs/x.md"}),
    ('NOTES = ROOT / "notes"\nread(NOTES / "y.txt")', {"notes/y.txt"}),
    ('read(ROOT / "a/b.md")', {"a/b.md"}),
    ('read(ROOT / "nodir" / "deeper" / "x.md")', {"nodir/deeper/x.md"}),
    ('list((ROOT / "nodir").glob("*.md"))', set()),
    ('list((ROOT / "nodir").rglob("*.md"))', set()),
    ('for d in ("README.md", "docs/MANUAL_SETUP.md"):\n    read(ROOT / d)', {"README.md", "docs/MANUAL_SETUP.md"}),
    ('names = ["no_such_file.md", "scripts", "two words.md"]', set()),
    ('read(ROOT / "docs")', set()),
    ('read(ROOT / f"{name}.md")', set()),
    ('read(other / "x.md")', set()),
    ('list((ROOT / "scripts").glob("plan_s*.py"))', {"scripts/plan_shots.py"}),
    ('S = ROOT / "scripts"\nlist(S.glob("ground_f*.py"))', {"scripts/ground_focus.py"}),
    ('list((ROOT / "docs").rglob("MANUAL_SETUP.md"))', {"docs/MANUAL_SETUP.md"}),
    ('list((ROOT / "scripts").glob("no_such_*.py"))', set())])
def test_read_derivation_understands_the_path_idioms_of_this_module(source, expected):
    assert repo_files_read_by(source) == expected


FILES_THAT_MUST_BE_SEEN = {
    "README.md", "docs/MANUAL_SETUP.md", ".env.example", "requirements.txt", "requirements-dev.txt", "pytest.ini",
    ".github/workflows/publish.yml", ".github/workflows/test.yml", "scripts/plan_shots.py", "tests/test_workflows.py"}


def test_this_module_reports_the_repo_files_it_reads():
    assert FILES_THAT_MUST_BE_SEEN <= repo_files_read_by_this_module()


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_test_workflow_runs_when_any_file_these_tests_read_changes(event):
    paths = (load_test_workflow().get("on") or load_test_workflow().get(True))[event]["paths"]
    uncovered = sorted(p for p in repo_files_read_by_this_module() if not path_matches(p, paths))
    assert not uncovered, f"edits to {uncovered} would not trigger tests.yml on {event}"


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_test_workflow_paths_name_the_workflow_docs_and_env_example_files(event):
    paths = (load_test_workflow().get("on") or load_test_workflow().get(True))[event]["paths"]
    assert {".github/workflows/publish.yml", "README.md", ".env.example", "docs/MANUAL_SETUP.md"} <= set(paths)
    assert len(paths) == len(set(paths))


# --- requirements ---------------------------------------------------------------------------------------------------------

def test_every_third_party_module_the_scripts_import_is_in_requirements_txt():
    installed = requirement_names(ROOT / "requirements.txt")
    missing = []
    for path in sorted(SCRIPTS.glob("*.py")):
        for module, guarded in third_party_imports(path):
            dists = dists_for(module)
            assert dists, f"{path.name} imports {module}: add it to DIST_FOR_MODULE"
            if not guarded and not dists & installed:
                missing.append(f"{path.name}: {module} -> {sorted(dists)}")
    assert not missing, f"add to requirements.txt: {missing}"


def test_every_third_party_module_the_tests_import_is_in_the_requirements_files():
    installed = requirement_names(ROOT / "requirements.txt") | requirement_names(ROOT / "requirements-dev.txt")
    missing = []
    for path in sorted(TESTS.glob("*.py")):
        for module, _ in third_party_imports(path):
            dists = dists_for(module)
            assert dists, f"{path.name} imports {module}: add it to DIST_FOR_MODULE"
            if not dists & installed:
                missing.append(f"{path.name}: {module} -> {sorted(dists)}")
    assert not missing, f"add to requirements-dev.txt: {missing}"


def test_pytest_timeout_setting_has_its_plugin_in_requirements_dev():
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    assert "timeout" in parser["pytest"]
    assert "pytest-timeout" in requirement_names(ROOT / "requirements-dev.txt")


# --- README -----------------------------------------------------------------------------------------------------------------

def readme():
    return read(ROOT / "README.md")


def video_section():
    text = readme()
    heading, faq = re.compile(r"^## 影片合成模式$", re.M), re.compile(r"^## 常見問題$", re.M)
    assert len(heading.findall(text)) == 1 and len(faq.findall(text)) == 1
    start, end = heading.search(text).start(), faq.search(text).start()
    assert start < end
    return text[start:end]


def table_rows(section):
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in section.splitlines()
            if line.startswith("|") and not re.fullmatch(r"\|[-| ]+\|", line.strip())]


def code_defaults(monkeypatch):
    import generate_images
    import notify
    import plan_shots
    import visual_style
    monkeypatch.delenv("MAX_IMAGES", raising=False)
    crf = re.search(r'env\("VIDEO_CRF", "(\d+)"', read(SCRIPTS / "render_video.py")).group(1)
    return {"VIDEO_MODE": "slideshow", "VISUAL_STYLE": visual_style.DEFAULT_STYLE,
            "MAX_IMAGES": str(plan_shots.max_images()), "IMAGE_THROTTLE_SEC": str(generate_images.THROTTLE_BASE_SEC),
            "IMAGE_UNIT_COST_USD": str(notify.DEFAULT_IMAGE_COST_USD), "VIDEO_CRF": crf}


def test_readme_video_section_sits_above_the_faq_as_a_single_h2():
    assert not re.search(r"^## ", video_section()[len("## 影片合成模式"):], re.M)


def test_readme_documents_every_video_variable_and_the_code_reads_each_one():
    rows = table_rows(video_section())
    documented = {name for row in rows for name in re.findall(r"`([A-Z][A-Z0-9_]+)`", row[0])}
    assert documented == DOCUMENTED_ENV
    assert documented <= env_names_read_by_scripts()
    assert {"`shots` (預設)", "`slideshow`"} <= {row[0] for row in rows}


def test_readme_defaults_match_the_code(monkeypatch):
    rows = {row[0].strip("`"): row for row in table_rows(video_section())}
    for name, value in code_defaults(monkeypatch).items():
        if name != "VIDEO_MODE":
            assert rows[name][1].strip("`") == value, name
    assert re.search(r'env\("RENDER_WORKERS", str\(min\(4, os\.cpu_count\(\) or 1\)\)',
                     read(SCRIPTS / "render_video.py"))
    assert rows["RENDER_WORKERS"][1].strip("`") == "min(4, CPU 數)"


def test_readme_dry_run_commands_are_exact_and_use_real_flags_and_stages():
    block = re.search(r"```bash\n(.*?)\n```", video_section().split("### 本機 dry-run", 1)[1], re.S).group(1)
    lines = [line.split("#")[0].strip() for line in block.splitlines()]
    commands = ["python scripts/run_episode_local.py --topic-id 161 --short",
                "python scripts/run_episode_local.py --topic-id 161 --from-stage images",
                "python scripts/run_episode_local.py --topic-id 161 --only video --limit-seconds 30"]
    assert lines == ["az login", "cp .env.example .env"] + commands
    tree = parse(SCRIPTS / "run_episode_local.py")
    flags = {a.value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"
             for a in n.args if isinstance(a, ast.Constant) and str(a.value).startswith("--")}
    stages = next(ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "STAGES")
    for command in commands:
        assert set(re.findall(r"--[\w-]+", command)) <= flags, command
        assert all(stage in stages for stage in re.findall(r"--(?:from-stage|only) (\w+)", command)), command


def test_readme_lists_output_files_the_scripts_really_write(tmp_path, monkeypatch):
    import common
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    line = next(l for l in readme().splitlines() if l.startswith("成品在"))
    assert line.split(":")[0] == f"成品在 `output/{common.get_episode_dir(161).name}/`"
    names = set(re.findall(r"`([\w.]+\.(?:mp4|png|json|srt))`", line))
    assert names == {"final.mp4", "contact_sheet.png", "video_report.json", "plan_report.json", "image_report.json",
                     "subtitles.srt"}
    assert names <= script_string_literals()


# --- MANUAL_SETUP and cross-doc references ----------------------------------------------------------------------------------

def manual_setup():
    return read(ROOT / "docs" / "MANUAL_SETUP.md")


def h2_headings(text):
    return re.findall(r"^## (.+)$", text, re.M)


def section_bounds(text, letter, title):
    """(start, end) of the `## <letter>. <title>` section, asserting the heading is present exactly once."""
    heading = re.compile(rf"^## {letter}\. {re.escape(title)}$", re.M)
    assert len(heading.findall(text)) == 1, f"{letter}. {title}"
    start = heading.search(text).start()
    following = re.search(r"^## ", text[start + 3:], re.M)
    return start, start + 3 + following.start() if following else len(text)


BUDGET_TITLE, FIRST_RUN_TITLE, DISCORD_TITLE = "Azure 預算警示 (建議)", "第一次在 GitHub 上跑分鏡模式", "(選用) Discord 通知"


def test_manual_setup_documents_budget_alerts_and_the_first_shots_run():
    text = manual_setup()
    budget_start, budget_end = section_bounds(text, "F", BUDGET_TITLE)
    first_run_start, first_run_end = section_bounds(text, "G", FIRST_RUN_TITLE)
    assert budget_end == first_run_start
    budget_text, first_run_text = text[budget_start:budget_end], text[first_run_start:first_run_end]
    assert "`IMAGE_UNIT_COST_USD`" in budget_text and "`MAX_IMAGES`" in budget_text
    assert "`SUBTITLE_FONT_INDEX`" in first_run_text and "`duration_ok`" in first_run_text
    assert {"IMAGE_UNIT_COST_USD", "MAX_IMAGES", "SUBTITLE_FONT_INDEX"} <= env_names_read_by_scripts()
    assert "duration_ok" in dict_keys_written_by(SCRIPTS / "render_video.py")


def test_manual_setup_states_the_cost_budget_and_acceptance_numbers_of_the_spec():
    text = manual_setup()
    budget_start, budget_end = section_bounds(text, "F", BUDGET_TITLE)
    first_run_start, first_run_end = section_bounds(text, "G", FIRST_RUN_TITLE)
    budget_text, first_run_text = text[budget_start:budget_end], text[first_run_start:first_run_end]
    assert "分鏡模式每集約 $2–3 (圖片約 $1.5–2)" in budget_text   # spec section 9: total $2–3 per episode, images $1.5–2
    assert "Amount 填 `100` (USD)" in budget_text and "重置週期 Monthly" in budget_text
    assert "Actual 80% 與 100%" in budget_text
    assert "備援鏡頭 ≤ 10%" in first_run_text


def readme_test_run_section():
    text = readme()
    heading, faq = re.compile(r"^### GitHub Actions 手動測試$", re.M), re.compile(r"^## 常見問題$", re.M)
    assert len(heading.findall(text)) == 1 and len(faq.findall(text)) == 1
    start = heading.search(text).start()
    return text[start:faq.search(text, start).start()]


def first_shots_run_section():
    text = manual_setup()
    start, end = section_bounds(text, "G", FIRST_RUN_TITLE)
    return text[start:end]


def readme_row(name):
    return next(row for row in table_rows(video_section()) if row[0] in (f"`{name}`", f"`{name}` (預設)"))


TEST_RUN_FACTS = ["`upload_youtube.py`", "仍會", "`unlisted`", "`private`", "`published_log.json`", "同一個題目",
                  "60–90 分鐘"]
SOFT_CAP_FACTS = ["`MAX_IMAGES`", "軟上限", "1.5 倍", "`budget.exceeded`"]


@pytest.mark.parametrize("doc", [readme_test_run_section, first_shots_run_section], ids=["README", "MANUAL_SETUP-G"])
def test_both_docs_say_what_a_manual_test_run_really_does(doc):
    text = doc()
    for fact in TEST_RUN_FACTS:
        assert fact in text, fact
    assert "取消勾選 `publish_podcast`" in text and "`video_mode=shots`" in text and "`privacy=private`" in text


def test_the_soft_cap_is_documented_where_images_are_budgeted():
    row = readme_row("MAX_IMAGES")
    assert row[1] == "`24`"
    for fact in ["每集圖片張數上限", "場景最多 4 個鏡頭", "每集下限約 20 張", *SOFT_CAP_FACTS[1:]]:
        assert fact in row[2], fact
    for fact in SOFT_CAP_FACTS:
        assert fact in first_shots_run_section(), fact
    budget_start, budget_end = section_bounds(manual_setup(), "F", BUDGET_TITLE)
    assert "軟上限" in manual_setup()[budget_start:budget_end] and "1.5 倍" in manual_setup()[budget_start:budget_end]
    assert {"budget", "exceeded"} <= dict_keys_written_by(SCRIPTS / "plan_shots.py")   # the key the docs point at


def test_the_documented_test_run_behaviour_is_what_the_workflow_does():
    assert "if" not in named_step("上傳 YouTube")   # the upload does not look at publish_podcast: a test run still uploads
    assert named_step(PODCAST_STEP)["if"] == PODCAST_IF
    callers = [path.name for path in SCRIPTS.glob("*.py") if path.name != "common.py" and "mark_published(" in read(path)]
    assert callers == ["publish_podcast.py"]   # nothing else writes published_log.json, so an unpublished run leaves it
    assert "published_log.json" in named_step(COMMIT_STEP)["run"]
    assert TRIGGERS["workflow_dispatch"]["inputs"]["privacy"]["default"] == "unlisted"
    assert PUBLISH["jobs"]["publish"]["timeout-minutes"] >= 120   # room for the documented 60-90 minutes plus slack


@pytest.mark.parametrize("doc", [readme, manual_setup], ids=["README", "MANUAL_SETUP"])
def test_the_wait_time_figures_are_labelled_per_mode(doc):
    lines = [line for line in doc().splitlines() if "等 ~60–90 分鐘" in line]
    assert len(lines) == 1
    assert "預設的 `shots` 模式" in lines[0] and "`slideshow` 模式約 ~10-15 分鐘" in lines[0]


def test_readme_promises_automatic_degradation_only_for_the_render_step():
    text = readme_row("shots")[1]
    assert "渲染失敗時自動降級為 `slideshow`" in text
    assert all(word in text for word in ("規劃", "產圖", "聚焦", "該次執行失敗"))
    assert "失敗時自動降級" not in text.replace("渲染失敗時自動降級", "")
    for part in ("規劃分鏡", "生成插畫", "聚焦定位"):   # nothing in the workflow absorbs a failure of these steps
        assert "continue-on-error" not in named_step(part) and "if" not in named_step(part), part


def test_documented_noto_font_index_is_the_one_the_code_uses():
    import render_overlays
    path, index = render_overlays.REGULAR_CANDIDATES[0]
    assert "NotoSansCJK" in path and render_overlays.BOLD_CANDIDATES[0][1] == index
    assert re.findall(r"Noto Sans CJK TC = (\d+)", readme()) == [str(index)]
    assert re.findall(r"Noto Sans CJK TC 索引預設為 (\d+)", manual_setup()) == [str(index)]


def test_manual_setup_sections_are_lettered_once_in_order_and_sit_above_the_wrap_up_sections():
    headings = h2_headings(manual_setup())
    assert [h[:2] for h in headings if re.match(r"[A-Z]\. ", h)] == [f"{letter}." for letter in "ABCDEFG"]
    assert headings[-3:] == ["完成後檢查清單", "之後的維運", "常見錯誤"]
    assert headings[-4] == f"G. {FIRST_RUN_TITLE}" and headings[-5] == f"F. {BUDGET_TITLE}"
    assert headings[-6] == f"E. {DISCORD_TITLE}"


def test_manual_setup_checklist_lists_every_lettered_section_in_order():
    text = manual_setup()
    checklist = text[:text.index("## A. ")]
    items = re.findall(r"^- \[ \] \*\*([A-Z])\. (.+)\*\*$", checklist, re.M)
    assert [letter for letter, _ in items] == list("ABCDEFG")
    assert dict(items)["E"] == DISCORD_TITLE
    assert dict(items)["F"] == BUDGET_TITLE and dict(items)["G"] == FIRST_RUN_TITLE


def test_docs_say_the_schedule_runs_shots_with_cinematic_and_how_to_go_back():
    assert "排程觸發預設使用 `shots` + `cinematic`" in readme()
    assert "手動執行時選 `video_mode=slideshow`" in readme()
    assert "排程預設已是 `shots` + `cinematic`" in manual_setup()
    assert "手動執行時選 `video_mode=slideshow`" in manual_setup()
    assert "當天就不會有影片" in readme()
    assert "才把排程預設改為" not in manual_setup()


@pytest.mark.parametrize("doc", ["README.md", "docs/MANUAL_SETUP.md"])
def test_docs_name_real_workflow_inputs_and_the_review_artifact(doc):
    text = read(ROOT / doc)
    inputs = TRIGGERS["workflow_dispatch"]["inputs"]
    chosen = [(k, v) for k, v in re.findall(r"`(\w+)=(\w+)`", text) if k in inputs]
    assert ("video_mode", "shots") in chosen and ("privacy", "private") in chosen
    assert all(value in inputs[key]["options"] for key, value in chosen), chosen
    assert "`publish_podcast`" in text and inputs["publish_podcast"]["type"] == "boolean"
    assert named_step("review artifacts")["with"]["name"].replace("-" + RUN_SUFFIX, "-*") in text


# --- .env.example -----------------------------------------------------------------------------------------------------------

DRY_RUN_MARKER = "# --- 本機 dry-run"
DRY_RUN_VALUES = {
    "AZURE_OPENAI_ENDPOINT": "https://openai-storyteller-ai-history.openai.azure.com/",
    "AZURE_OPENAI_GPT_DEPLOYMENT": "gpt-5-mini", "AZURE_OPENAI_IMAGE_DEPLOYMENT": "gpt-image-2",
    "AZURE_SPEECH_REGION": "eastus", "AZURE_SPEECH_CUSTOM_DOMAIN": "speech-storyteller-ai-history",
    "AZURE_SPEECH_VOICE": "zh-CN-YunjianNeural"}


def env_example_text():
    return read(ROOT / ".env.example")


def test_env_example_new_sections_only_name_variables_the_code_reads():
    new_text = env_example_text().split(DRY_RUN_MARKER, 1)[1]
    names = set(re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", new_text))
    assert {"AZURE_OPENAI_API_VERSION", "VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES"} <= names
    assert names <= env_names_read_by_scripts(), sorted(names - env_names_read_by_scripts())


def test_env_example_effective_values_are_the_dry_run_ones_and_the_code_defaults(monkeypatch):
    values = dotenv_values(ROOT / ".env.example")
    assert {k: values[k] for k in DRY_RUN_VALUES} == DRY_RUN_VALUES
    defaults = code_defaults(monkeypatch)
    assert {k: values[k] for k in ("VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES")} == {
        k: defaults[k] for k in ("VIDEO_MODE", "VISUAL_STYLE", "MAX_IMAGES")}


def test_env_example_tells_local_runs_not_to_set_the_api_version():
    text = env_example_text()
    assert re.search(r"^# AZURE_OPENAI_API_VERSION .*本機請不要設定", text, re.M)
    assert "AZURE_OPENAI_API_VERSION" not in dotenv_values(ROOT / ".env.example")   # copying it to .env must not set it


def test_env_example_contains_placeholders_only_no_secrets():
    text = env_example_text()
    for name, value in dotenv_values(ROOT / ".env.example").items():
        if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION_STRING", name):
            assert not value or value.startswith("your_") or "..." in value, name
    assert not re.search(r"[0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}|\bsk-\w{16,}|eyJ[\w-]{10,}", text)
    new_names = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text.split(DRY_RUN_MARKER, 1)[1], re.M))
    assert not [n for n in new_names if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CONNECTION_STRING", n)]


LEGACY_UNUSED_VARIABLES = ["AZURE_OPENAI_KEY", "AZURE_OPENAI_DALLE_DEPLOYMENT", "AZURE_SPEECH_KEY"]


def test_env_example_header_does_not_ask_for_keys_and_points_at_the_dry_run_block():
    header = env_example_text().split("\n\n", 1)[0]
    assert header.startswith("# ====") and header.endswith("====")
    assert "填入金鑰" not in header
    assert "az login" in header
    assert "「本機 dry-run」區塊" in header and "本機 dry-run" in DRY_RUN_MARKER   # names the block that really exists


def test_env_example_marks_the_legacy_variables_that_no_script_reads_as_unused():
    read_names = env_names_read_by_scripts()
    assignments = [(m.group(1), line) for line in env_example_text().splitlines()
                   if (m := re.match(r"#?\s*([A-Z][A-Z0-9_]+)=", line))]   # a variable may be assigned more than once
    for name in LEGACY_UNUSED_VARIABLES:
        assert name not in read_names, f"{name} is read by a script now: drop its 未使用 mark"
        lines = [line for assigned, line in assignments if assigned == name]
        assert lines and all("未使用" in line for line in lines), name
    assert not [(name, line) for name, line in assignments if "未使用" in line and name in read_names]


# --- CI skip guard (tests/conftest.py) -----------------------------------------------------------------------------------
# A runner that lacks ffmpeg or a CJK font would otherwise skip every render test and stay green.

GUARDED_SKIP_REASONS = ("ffmpeg is not available", "no CJK font installed")
GUARD_MESSAGE = "tests skipped for missing tools"
FFMPEG_SKIP = "def test_render(ffmpeg_exe):\n    pass\n"
FONT_SKIP = 'import pytest\n\n\ndef test_render():\n    pytest.skip("no CJK font installed")\n'
OTHER_SKIP = 'import pytest\n\n\ndef test_other():\n    pytest.skip("not related to any tool")\n'
FONT_AND_OTHER_SKIP = FONT_SKIP + '\n\ndef test_other():\n    pytest.skip("not related to any tool")\n'
FONT_SKIP_AND_FAILURE = FONT_SKIP + "\n\ndef test_bad():\n    assert False\n"
PASSING = "def test_ok():\n    pass\n"
NO_FFMPEG = {"FFMPEG_BIN": "no-such-ffmpeg-binary"}


def run_inner_pytest(tmp_path, test_source, ci, extra_env=None):
    """Runs a throw-away project that uses a copy of tests/conftest.py; returns (exit code, terminal output)."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    shutil.copy(TESTS / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(test_source, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "FFMPEG_BIN", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env.update(PYTHONPATH=str(SCRIPTS), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", **(extra_env or {}))
    if ci is not None:
        env["CI"] = ci
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    return done.returncode, done.stdout + done.stderr


@pytest.mark.parametrize("source, ci, extra_env, exit_code, guarded", [
    (FFMPEG_SKIP, "true", NO_FFMPEG, 1, True),
    (FONT_SKIP, "true", None, 1, True),
    (FONT_SKIP, "1", None, 1, True),
    (FFMPEG_SKIP, None, NO_FFMPEG, 0, False),
    (FONT_SKIP, None, None, 0, False),
    (FONT_SKIP, "", None, 0, False),
    (FONT_SKIP, "false", None, 0, False),
    (FONT_SKIP, "0", None, 0, False),
    (OTHER_SKIP, "true", None, 0, False),
    (PASSING, "true", None, 0, False),
    (FONT_AND_OTHER_SKIP, "true", None, 1, True)],
    ids=["ffmpeg-ci", "font-ci", "font-ci-1", "ffmpeg-local", "font-local", "font-ci-empty", "font-ci-false",
         "font-ci-0", "unrelated-skip-ci", "all-pass-ci", "font-and-unrelated-ci"])
def test_ci_turns_skips_for_a_missing_ffmpeg_or_font_into_a_failed_session(
        tmp_path, source, ci, extra_env, exit_code, guarded):
    code, output = run_inner_pytest(tmp_path, source, ci, extra_env)
    assert code == exit_code, output
    assert (GUARD_MESSAGE in output) is guarded, output
    if source != PASSING:
        assert "skipped" in output   # the inner run really skipped; the guard only reads that
    if guarded:
        assert "test_inner.py::test_render" in output and "CI" in output
        assert "test_inner.py::test_other" not in output   # an unrelated skip is not listed as a missing tool


def test_the_guard_keeps_real_failures_failing_and_still_reports_the_missing_tool(tmp_path):
    code, output = run_inner_pytest(tmp_path, FONT_SKIP_AND_FAILURE, "true")
    assert code == 1 and "1 failed" in output and GUARD_MESSAGE in output


SKIP_USES = re.compile(r"pytest\.(?:mark\.)?(?:skip|skipif|importorskip|xfail)\b[^\n]*")


def test_every_skip_in_the_suite_is_one_the_ci_guard_fails_on():
    uses = [(path.name, match.group(0).strip()) for path in sorted(TESTS.glob("*.py"))
            if path.name != "test_workflows.py" for match in SKIP_USES.finditer(read(path))]
    assert len(uses) >= 4   # conftest + the three font-dependent test modules; fewer means the scan is blind
    for name, use in uses:
        reason = re.fullmatch(r'pytest\.skip\("(.+)"\)', use)
        assert reason and reason.group(1) in GUARDED_SKIP_REASONS, f"{name}: {use} is not covered by the CI guard"
