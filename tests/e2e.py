import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import closing
from multiprocessing import Process, get_context
from multiprocessing.connection import wait
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest
import requests
import uvicorn
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Locator, Page, Route, expect

import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Which AI projects have you delivered?"
ANSWER = "I build reliable AI applications and developer tools."

expect.set_options(timeout=10_000)


def wait_for_server_ready(url: str, process: Process, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    with requests.Session() as client:
        client.trust_env = False
        while time.monotonic() < deadline:
            if not process.is_alive():
                raise RuntimeError(f"Browser test server exited with code {process.exitcode}")
            try:
                response = client.get(url, timeout=0.5)
                if response.status_code == 200:
                    return
            except requests.RequestException:
                pass
            wait([process.sentinel], timeout=min(0.1, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Browser test server did not become ready at {url}")


@pytest.fixture(scope="session")
def free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return server_socket.getsockname()[1]


def run_server(port: int) -> None:
    with mock.patch.dict(
        os.environ,
        {
            "AZURE_OPENAI_CUSTOM_URL": "http://127.0.0.1:1",
            "AZURE_OPENAI_API_KEY_OVERRIDE": "local-browser-test-only",
            "AZURE_OPENAI_CHATGPT_MODEL": "gpt-4.1-mini",
            "ENABLE_LANGUAGE_PICKER": "false",
            "USE_SPEECH_INPUT_BROWSER": "false",
            "USE_SPEECH_OUTPUT_BROWSER": "false",
            "USE_SPEECH_OUTPUT_AZURE": "false",
            "USE_CHAT_HISTORY_BROWSER": "false",
            "USE_CHAT_HISTORY_COSMOS": "false",
            "USE_REALTIME_VOICE": "false",
            "KNOWLEDGE_DATA_DIR": str(PROJECT_ROOT / "data" / "work"),
        },
        clear=True,
    ):
        with (
            mock.patch("app.AzureDeveloperCliCredential", return_value=mock.Mock(close=mock.AsyncMock())),
            mock.patch(
                "httpx.AsyncClient.send", side_effect=AssertionError("Backend HTTP is forbidden in browser tests")
            ),
            mock.patch(
                "aiohttp.ClientSession._request",
                side_effect=AssertionError("Backend HTTP is forbidden in browser tests"),
            ),
        ):
            uvicorn.run(app.create_app(), host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2)


@pytest.fixture(scope="session")
def live_server_url(free_port: int) -> Generator[str, None, None]:
    process = get_context("spawn").Process(target=run_server, args=(free_port,), daemon=True)
    process.start()
    url = f"http://127.0.0.1:{free_port}/"
    try:
        wait_for_server_ready(url, process)
        yield url
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "locale": "en-US",
        "reduced_motion": "reduce",
        "service_workers": "block",
        "viewport": {"width": 1280, "height": 900},
    }


@pytest.fixture(autouse=True)
def api_config(page: Page, live_server_url: str, public_profile, browser_requests, request):
    config = {
        "streamingEnabled": True,
        "showLanguagePicker": False,
        "showSpeechInput": False,
        "showSpeechOutputBrowser": False,
        "showSpeechOutputAzure": False,
        "showChatHistoryBrowser": False,
        "showChatHistoryCosmos": False,
        "showRealtimeVoice": False,
    }
    unexpected_requests = []

    def handle(route: Route):
        request_url = urlsplit(route.request.url)
        if route.request.resource_type == "font" and request_url.hostname in (
            "res-1.cdn.office.net",
            "res.cdn.office.net",
        ):
            route.abort()
        elif request_url.netloc != urlsplit(live_server_url).netloc:
            unexpected_requests.append(route.request.url)
            route.abort()
        elif request_url.path == "/config":
            route.fulfill(json=config)
        elif request_url.path == "/auth_setup_admin":
            route.fulfill(json={"enabled": False})
        elif request_url.path == "/api/status":
            route.fulfill(json=public_profile)
        elif route.request.method == "GET" and (
            request_url.path in ("/", "/avatar.svg", "/social-preview.png") or request_url.path.startswith("/assets/")
        ):
            route.continue_()
        else:
            unexpected_requests.append(f"{route.request.method} {route.request.url}")
            route.fulfill(status=501, json={"error": "Unmocked browser request"})

    page.context.route("**/*", handle)
    yield config
    capture_screenshot(page, re.sub(r"[^A-Za-z0-9_-]", "-", request.node.name))
    assert not unexpected_requests, f"Unmocked or external requests: {unexpected_requests}"


@pytest.fixture
def public_profile():
    return {
        "vacation_mode": False,
        "vacation_return_date": None,
        "owner_name": "Alex Morgan",
        "assistant_name": "Alex's AI assistant",
        "site_description": "Explore Alex's fictional projects and experience.",
        "welcome_message": "Welcome! Ask about Alex's projects, experience, or availability.",
        "owner_email": "",
        "calendar_url": "",
        "linkedin_url": "",
        "avatar_url": "/avatar.svg",
        "social_image_url": "/social-preview.png",
        "tone": "Friendly and clear.",
        "example_questions": [
            "What projects have you worked on?",
            "What are your main skills?",
            "Tell me about your experience.",
            "How can I get in touch?",
        ],
    }


@pytest.fixture
def browser_requests(page: Page):
    observed = []
    page.on(
        "request",
        lambda request: observed.append(
            {
                "path": urlsplit(request.url).path,
                "authorization": request.headers.get("authorization"),
            }
        ),
    )
    return observed


@pytest.fixture
def owner_auth(page: Page):
    page.route(
        "**/auth_setup_admin",
        lambda route: route.fulfill(
            json={
                "enabled": True,
                "msalConfig": {
                    "auth": {
                        "clientId": "11111111-1111-1111-1111-111111111111",
                        "authority": "https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222",
                        "redirectUri": "/redirect",
                    }
                },
            }
        ),
    )
    page.add_init_script("""window.ownerAuthMocks = {
        signIns: 0, signOuts: 0, pauseSignIn: false, cancelSignIn: false,
        resets: 0, failReset: false, signInErrorCode: null,
        account: {name: 'Example Owner', username: 'owner@example.test'}
    };""")
    page.route(
        re.compile(r"/assets/adminAuth-[^/]+\.js$"),
        lambda route: route.fulfill(
            content_type="application/javascript",
            body="""export default async () => ({
                getCachedToken: async () => window.ownerAuthMocks.cachedToken,
                getToken: async () => 'browser-owner-id-token',
                getAccount: () => window.ownerAuthMocks.account,
                signIn: async () => {
                    window.ownerAuthMocks.signIns++;
                    if (window.ownerAuthMocks.pauseSignIn) {
                        await new Promise(resolve => {window.ownerAuthMocks.finishSignIn = resolve;});
                    }
                    if (window.ownerAuthMocks.cancelSignIn) throw {errorCode: 'user_cancelled'};
                    if (window.ownerAuthMocks.signInErrorCode) throw {errorCode: window.ownerAuthMocks.signInErrorCode};
                    return 'browser-owner-id-token';
                },
                resetSignIn: async () => {
                    window.ownerAuthMocks.resets++;
                    if (window.ownerAuthMocks.failReset) throw new Error('Reset failed');
                    window.ownerAuthMocks.cachedToken = undefined;
                },
                signOut: async () => {window.ownerAuthMocks.signOuts++;}
            });""",
        ),
    )


@pytest.fixture
def real_owner_auth(page: Page):
    client_id = "11111111-1111-1111-1111-111111111111"
    authority = "https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222"
    metadata = {
        "authorization_endpoint": f"{authority}/oauth2/v2.0/authorize",
        "token_endpoint": f"{authority}/oauth2/v2.0/token",
        "end_session_endpoint": f"{authority}/oauth2/v2.0/logout",
        "issuer": f"{authority}/v2.0",
        "jwks_uri": f"{authority}/discovery/v2.0/keys",
    }
    page.route(
        "**/auth_setup_admin",
        lambda route: route.fulfill(
            json={
                "enabled": True,
                "msalConfig": {
                    "auth": {
                        "clientId": client_id,
                        "authority": authority,
                        "redirectUri": "/redirect",
                        "authorityMetadata": json.dumps(metadata),
                        "cloudDiscoveryMetadata": json.dumps(
                            {
                                "tenant_discovery_endpoint": f"{authority}/v2.0/.well-known/openid-configuration",
                                "metadata": [
                                    {
                                        "preferred_network": "login.microsoftonline.com",
                                        "preferred_cache": "login.microsoftonline.com",
                                        "aliases": ["login.microsoftonline.com"],
                                    }
                                ],
                            }
                        ),
                    },
                    "cache": {"cacheLocation": "sessionStorage"},
                },
            }
        ),
    )

    def provider(route: Route):
        assert urlsplit(route.request.url).path.endswith("/oauth2/v2.0/authorize"), route.request.url
        route.fulfill(content_type="text/html", body="<h1>Microsoft sign-in test provider</h1>")

    page.context.route("https://login.microsoftonline.com/**", provider)
    return client_id


@pytest.fixture
def owner_settings(page: Page, public_profile, owner_auth):
    state = {
        "settings": {**public_profile, "manager_email": "", "fun_facts": ["Enjoys clear documentation."]},
        "updates": [],
    }

    def handle(route: Route):
        if route.request.method == "PUT":
            state["updates"].append(route.request.post_data_json)
            state["settings"].update(route.request.post_data_json)
        route.fulfill(json=state["settings"], headers={"ETag": '"owner-workspace-version-1"'})

    page.route("**/api/admin/settings", handle)
    return state


def capture_screenshot(page: Page, name: str) -> None:
    directory = PROJECT_ROOT / ".pytest_cache" / "test-results"
    directory.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(directory / f"{name}.png"), full_page=True, animations="disabled")


def assert_text_contrast(locator: Locator) -> None:
    expect(locator).to_be_visible()
    locator.scroll_into_view_if_needed()
    measured = locator.evaluate("""element => {
        const rgba = color => color.match(/[\\d.]+/g).map(Number);
        const blend = (foreground, background) => {
            const alpha = foreground[3] ?? 1;
            return foreground.slice(0, 3).map((channel, index) =>
                channel * alpha + background[index] * (1 - alpha));
        };
        const ancestors = [];
        for (let node = element; node; node = node.parentElement) ancestors.unshift(node);
        const background = ancestors.reduce((color, node) =>
            blend(rgba(getComputedStyle(node).backgroundColor), color), [255, 255, 255]);
        const foreground = blend(rgba(getComputedStyle(element).color), background);
        const luminance = color => color.map(channel => {
            const srgb = channel / 255;
            return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
        const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
        return {
            text: element.value || element.textContent.trim(),
            foreground,
            background,
            ratio: (values[0] + 0.05) / (values[1] + 0.05)
        };
    }""")
    assert measured["ratio"] >= 4.5, f"Text contrast is below WCAG AA: {measured}"


def chat_response(content: str = ANSWER, session_state="browser-session", context=None):
    return {
        "message": {"role": "assistant", "content": content},
        "session_state": session_state,
        "context": {
            "data_points": {"text": [], "images": [], "citations": []},
            "thoughts": [],
            "followup_questions": None,
            **(context or {}),
        },
    }


def stream_body(response: dict) -> str:
    content = response["message"]["content"]
    midpoint = len(content) // 2
    events = [
        {
            "delta": {"role": "assistant", "content": ""},
            "context": response["context"],
            "session_state": response["session_state"],
        },
        {"delta": {"content": content[:midpoint]}},
        {"delta": {"content": content[midpoint:]}},
        {"delta": {"content": None, "role": None}},
    ]
    return "".join(json.dumps(event) + "\n" for event in events)


def mock_chat(page: Page, responses: list[dict]):
    captured = []

    def handle(route: Route):
        captured.append(
            {
                "path": urlsplit(route.request.url).path,
                "method": route.request.method,
                "body": route.request.post_data_json,
                "content_type": route.request.headers.get("content-type"),
                "authorization": route.request.headers.get("authorization"),
            }
        )
        response = responses[min(len(captured) - 1, len(responses) - 1)]
        if urlsplit(route.request.url).path == "/chat/stream":
            route.fulfill(content_type="application/x-ndjson", body=stream_body(response))
        else:
            route.fulfill(json=response)

    page.route(re.compile(r"/chat(?:/stream)?$"), handle)
    return captured


def send_question(page: Page, question: str = QUESTION) -> None:
    submit_button = page.get_by_role("button", name="Submit question", exact=True)
    expect(submit_button).to_be_visible()
    page.get_by_role("textbox").fill(question)
    submit_button.click()


def expect_app_text(page: Page, text: str | re.Pattern) -> None:
    expected_text = page.get_by_text(text, exact=True)
    error_boundary = page.get_by_role("heading", name="Unexpected Application Error!", exact=True)
    expect(expected_text.or_(error_boundary)).to_be_visible()
    assert not error_boundary.is_visible(), page.locator("body").inner_text()
    expect(expected_text).to_be_visible()


def test_home(page: Page, live_server_url: str):
    page.goto(live_server_url)
    expect(page).to_have_title("Alex's AI assistant")
    expect(page.get_by_role("main").get_by_alt_text("Alex Morgan's avatar", exact=True)).to_be_visible()
    expect(page.get_by_placeholder("Type a question", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Clear chat")).to_be_disabled()
    expect(page.get_by_role("button", name="Submit question")).to_be_disabled()
    expect(page.get_by_role("button", name="Developer settings")).to_have_count(0)
    expect(page.get_by_role("button", name="Open chat history")).to_have_count(0)
    expect(page.get_by_role("button", name="Ask question with voice")).to_have_count(0)
    page.get_by_role("textbox").fill("   ")
    expect(page.get_by_role("button", name="Submit question")).to_be_disabled()


def test_home_accessibility(page: Page, live_server_url: str):
    page.goto(live_server_url)
    expect(page.get_by_role("main").get_by_alt_text("Alex Morgan's avatar", exact=True)).to_be_visible()
    results = Axe().run(page)
    assert results.violations_count == 0, results.generate_report()


@pytest.mark.parametrize(
    "profile_changes",
    [
        {
            "owner_name": "Alex Morgan",
            "assistant_name": "Alex's AI assistant",
            "site_description": "Explore Alex's fictional projects and experience.",
            "welcome_message": "Welcome to Alex's assistant.",
            "owner_email": "alex@example.test",
            "calendar_url": "https://calendar.example.test/alex",
            "linkedin_url": "https://www.linkedin.com/in/alex-example",
            "avatar_url": "/avatar.svg?persona=alex",
            "social_image_url": "/social-preview.png?persona=alex",
            "example_questions": ["Tell me about Alex's projects.", "What are Alex's interests?"],
        },
        {
            "owner_name": "Jamie Rivera",
            "assistant_name": 'Jamie’s "Studio" <Assistant>',
            "site_description": 'Design, research & "thoughtful" projects <for everyone>.',
            "welcome_message": "Welcome to Jamie's creative practice.",
            "owner_email": "jamie@example.test",
            "calendar_url": "https://calendar.example.test/jamie?topic=design&duration=30",
            "linkedin_url": "https://www.linkedin.com/in/jamie-example",
            "avatar_url": "/avatar.svg?persona=jamie",
            "social_image_url": "/social-preview.png?persona=jamie&mode=studio",
            "example_questions": ["What has Jamie designed?", "Tell me about Jamie's research."],
        },
    ],
    ids=["alex", "jamie"],
)
def test_profile_drives_public_ui_metadata_and_examples(
    page: Page, live_server_url: str, public_profile, profile_changes
):
    public_profile.update(profile_changes)
    captured = mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    expect(page).to_have_title(public_profile["assistant_name"])
    expect(page.get_by_role("heading", name=public_profile["assistant_name"], exact=True).last).to_be_visible()
    expect(page.get_by_text(public_profile["welcome_message"], exact=True)).to_be_visible()
    expect(page.get_by_alt_text(f"{public_profile['owner_name']}'s avatar", exact=True)).to_have_attribute(
        "src", public_profile["avatar_url"]
    )
    expect(page.get_by_role("banner").locator("img")).to_have_attribute("src", public_profile["avatar_url"])
    for selector, value in (
        ('meta[name="description"]', public_profile["site_description"]),
        ('meta[property="og:title"]', public_profile["assistant_name"]),
        ('meta[property="og:description"]', public_profile["site_description"]),
        ('meta[property="og:image"]', public_profile["social_image_url"]),
        ('meta[name="twitter:title"]', public_profile["assistant_name"]),
        ('meta[name="twitter:description"]', public_profile["site_description"]),
        ('meta[name="twitter:image"]', public_profile["social_image_url"]),
    ):
        expect(page.locator(selector)).to_have_count(1)
        expect(page.locator(selector)).to_have_attribute("content", value)
    for label, href in (
        ("Email the owner", f"mailto:{public_profile['owner_email']}"),
        ("Book a call", public_profile["calendar_url"]),
        ("LinkedIn", public_profile["linkedin_url"]),
    ):
        expect(page.get_by_role("link", name=label, exact=True)).to_have_attribute("href", href)
    page.get_by_role("button", name=public_profile["example_questions"][0], exact=True).click()
    expect_app_text(page, ANSWER)
    assert captured[0]["body"]["context"]["example_question_id"] == "example-1"
    assert captured[0]["body"]["messages"] == [{"role": "user", "content": public_profile["example_questions"][0]}]
    expect(page.get_by_role("main").locator('img[aria-hidden="true"]')).to_have_attribute(
        "src", public_profile["avatar_url"]
    )


@pytest.mark.parametrize("available_link", ["none", "linkedin"])
def test_optional_profile_links_are_omitted(page: Page, live_server_url: str, public_profile, available_link):
    public_profile.update(example_questions=[], owner_email="", calendar_url="", linkedin_url="")
    if available_link == "linkedin":
        public_profile["linkedin_url"] = "https://www.linkedin.com/in/fictional-owner"
    page.goto(live_server_url)
    expect(page.get_by_role("heading", name=public_profile["assistant_name"], exact=True).last).to_be_visible()
    expect(page.get_by_role("link", name="Email the owner", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Book a call", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="What projects have you worked on?", exact=True)).to_have_count(0)
    expect(page.get_by_role("contentinfo")).to_have_count(1 if available_link == "linkedin" else 0)
    expect(page.get_by_role("link", name="LinkedIn", exact=True)).to_have_count(
        1 if available_link == "linkedin" else 0
    )


def test_profile_failure_uses_neutral_fallback(page: Page, live_server_url: str):
    page.route("**/api/status", lambda route: route.fulfill(status=503, json={"error": "Profile unavailable"}))
    page.goto(live_server_url)
    expect(page).to_have_title("Persona Assistant")
    expect(page.get_by_role("heading", name="Persona Assistant", exact=True).last).to_be_visible()
    expect(page.get_by_role("contentinfo")).to_have_count(0)
    expect(page.get_by_role("main").get_by_role("img")).to_have_attribute("src", "/avatar.svg")
    expect(page.locator('meta[property="og:image"]')).to_have_attribute("content", "/social-preview.png")
    expect(page.locator('meta[name="twitter:image"]')).to_have_attribute("content", "/social-preview.png")


def test_vite_metadata_resolves_relative_profile_path_from_repository_root():
    profile_path = Path(".pytest_cache") / f"frontend-profile-path-{os.getpid()}.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(
            {
                "assistant_name": 'Casey <Guide> & "Studio" $&',
                "site_description": 'Design </title><script>alert(1)</script> & "research"',
                "social_image_url": "",
            }
        ),
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                """
                import assert from 'node:assert/strict';
                import {readFileSync} from 'node:fs';
                import {resolve} from 'node:path';
                import {createServer} from 'vite';
                const server = await createServer({
                    configFile: resolve('vite.config.ts'),
                    server: {middlewareMode: true}
                });
                try {
                    const html = await server.transformIndexHtml('/', readFileSync('index.html', 'utf8'));
                    assert(html.includes('Casey &lt;Guide&gt; &amp; &quot;Studio&quot; $&'));
                    assert(html.includes('Design &lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;research&quot;'));
                    assert(html.includes('content="/social-preview.png"'));
                    assert(!html.includes('__PROFILE_'));
                    assert(!html.includes('<script>alert(1)</script>'));
                } finally {
                    await server.close();
                }
                """,
            ],
            cwd=PROJECT_ROOT / "app" / "frontend",
            env={**os.environ, "PROFILE_PATH": profile_path.as_posix()},
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        profile_path.unlink(missing_ok=True)


def test_public_chat_never_bootstraps_visitor_login(page: Page, live_server_url: str, browser_requests):
    captured = mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    expect(page.get_by_text(re.compile("This is an AI assistant, not the owner"))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("sign in|google|microsoft|linkedin", re.I))).to_have_count(0)
    send_question(page)
    expect_app_text(page, ANSWER)
    assert captured[0]["authorization"] is None
    assert "example_question_id" not in captured[0]["body"]["context"]
    assert not any(
        request["path"].startswith(("/auth_setup", "/.auth/"))
        or re.search(r"/(?:authentication|adminAuth)-", request["path"])
        for request in browser_requests
    )


def test_admin_disabled_is_safe_and_does_not_load_msal(page: Page, live_server_url: str, browser_requests):
    page.goto(f"{live_server_url}#/admin")
    expect(page.get_by_text("Owner workspace is not configured.", exact=True)).to_be_visible()
    page.get_by_text("Deployment setup help", exact=True).click()
    expect(page.get_by_text(re.compile("Public chat still works"))).to_be_visible()
    expect(page.get_by_role("button", name="Continue with Microsoft", exact=True)).to_have_count(0)
    assert not any(re.search(r"/(?:authentication|adminAuth)-", request["path"]) for request in browser_requests)
    page.get_by_role("banner").get_by_role("link", name="Alex's AI assistant", exact=True).click()
    expect(page.get_by_role("textbox", name="Your message", exact=True)).to_be_visible()


def test_admin_non_owner_denial_does_not_affect_public_chat(page: Page, live_server_url: str, owner_auth):
    captured = []
    public_calls = mock_chat(page, [chat_response()])

    def deny_settings(route: Route):
        captured.append(route.request.headers)
        route.fulfill(status=403, json={"error": "Private server diagnostic must not be shown"})

    page.route("**/api/admin/settings", deny_settings)
    page.goto(f"{live_server_url}#/admin")
    expect(page.get_by_role("button", name="Continue with Microsoft", exact=True)).to_be_visible()
    assert captured == []
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(
        page.get_by_text("This Microsoft account does not have owner access. Try a different account.", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("button", name="Use another Microsoft account", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_count(0)
    expect(page.get_by_text("Private server diagnostic must not be shown", exact=True)).to_have_count(0)
    assert len(captured) == 1
    assert captured[0]["authorization"] == "Bearer browser-owner-id-token"
    assert page.evaluate("window.ownerAuthMocks.signIns") == 1
    page.get_by_role("banner").get_by_role("link", name="Alex's AI assistant", exact=True).click()
    send_question(page)
    expect_app_text(page, ANSWER)
    assert public_calls[0]["authorization"] is None


def test_admin_owner_controls_have_readable_dark_theme_contrast(
    page: Page, live_server_url: str, public_profile, owner_auth
):
    settings = {**public_profile, "manager_email": "private-manager@example.test", "fun_facts": []}
    page.route(
        "**/api/admin/settings",
        lambda route: (
            route.fulfill(status=400, json={"error": "Invalid settings"})
            if route.request.method == "PUT"
            else route.fulfill(json=settings)
        ),
    )
    page.route(
        "**/api/admin/analytics",
        lambda route: route.fulfill(
            json={
                "total_queries": 4,
                "avg_response_time_ms": 25,
                "queries_by_day": [],
                "confidence_distribution": {"high": 1, "medium": 1, "low": 1, "unknown": 1},
                "recent_queries": [
                    {
                        "confidence_level": level,
                        "response_time_ms": 25,
                        "timestamp": 1_800_000_000_000,
                        "error": level == "low",
                    }
                    for level in ("high", "medium", "low", "unknown")
                ],
                "unique_users": 2,
                "example_question_rate": 25,
                "avg_session_depth": 2,
                "error_count": 1,
            }
        ),
    )
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_value(public_profile["owner_name"])
    admin = page.locator('[class*="adminContainer"]')
    assert_text_contrast(page.get_by_text("Owner Name", exact=True))
    assert_text_contrast(page.get_by_label("Owner Name", exact=True))
    for element in admin.locator("label, input, textarea, .ms-Pivot-text").all():
        assert_text_contrast(element)
    for name in ("Sign out", "Add Fun Fact"):
        assert_text_contrast(page.get_by_role("button", name=name, exact=True).locator(".ms-Button-label"))
    assert_text_contrast(page.get_by_text("Vacation Mode", exact=True))
    page.get_by_role("switch", name="Vacation Mode", exact=True).click()
    assert_text_contrast(page.get_by_text("Vacation Mode", exact=True))
    page.get_by_label("Assistant Name", exact=True).fill("A readable assistant name")
    assert_text_contrast(page.get_by_role("button", name="Save changes", exact=True).locator(".ms-Button-label"))
    page.get_by_role("button", name="Save changes", exact=True).click()
    save_error = page.get_by_text("Unable to save settings. Check the values and try again.", exact=True)
    expect(save_error).to_be_visible()
    assert_text_contrast(save_error)

    page.get_by_role("tab", name="Usage", exact=True).click()
    page.get_by_role("button", name="Load Analytics", exact=True).click()
    expect(page.get_by_text("Sessions", exact=True)).to_be_visible()
    for element in admin.locator(
        '.ms-Pivot-text, [class*="analyticsTab"] h3, [class*="analyticsTab"] h4, '
        '[class*="analyticsTab"] p, [class*="analyticsTab"] .ms-Button-label, '
        '[class*="statValue"], [class*="statLabel"], [class*="legendItem"], '
        '[class*="queryText"], [class*="queryMeta"] > span'
    ).all():
        assert_text_contrast(element)

    page.get_by_role("banner").get_by_role("link", name="Alex's AI assistant", exact=True).click()
    page.get_by_role("button", name="Discard and continue", exact=True).click()
    expect(page.get_by_role("textbox", name="Your message", exact=True)).to_be_visible()
    assert page.locator("html").evaluate("element => getComputedStyle(element).backgroundColor") == "rgb(26, 26, 46)"
    assert page.locator("html").evaluate("element => getComputedStyle(element).color") == "rgb(242, 242, 242)"


def test_admin_saves_only_changed_fields_and_refreshes_profile(
    page: Page, live_server_url: str, public_profile, owner_auth
):
    settings = {**public_profile, "manager_email": "private-manager@example.test", "fun_facts": []}
    updates = []
    public_calls = mock_chat(page, [chat_response()])

    def handle_settings(route: Route):
        if route.request.method == "PUT":
            updates.append({"body": route.request.post_data_json, "headers": route.request.headers})
            settings.update(route.request.post_data_json)
            public_profile["assistant_name"] = settings["assistant_name"]
        route.fulfill(json=settings, headers={"ETag": '"profile-revision-1"'})

    page.route("**/api/admin/settings", handle_settings)
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(page.get_by_role("heading", name="Owner workspace", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Save changes", exact=True)).to_be_disabled()
    page.get_by_label("Assistant Name", exact=True).fill("Alex's updated assistant")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Changes saved. Your assistant is up to date.", exact=True)).to_be_visible()
    assert len(updates) == 1
    assert updates[0]["body"] == {"assistant_name": "Alex's updated assistant"}
    assert updates[0]["headers"]["if-match"] == '"profile-revision-1"'
    assert updates[0]["headers"]["authorization"] == "Bearer browser-owner-id-token"
    expect(page.get_by_role("banner").get_by_role("link", name="Alex's updated assistant", exact=True)).to_be_visible()
    page.get_by_role("banner").get_by_role("link", name="Alex's updated assistant", exact=True).click()
    send_question(page)
    expect_app_text(page, ANSWER)
    assert public_calls[0]["authorization"] is None


def test_admin_analytics_labels_sessions_and_omits_raw_questions(
    page: Page, live_server_url: str, public_profile, owner_auth
):
    page.route(
        "**/api/admin/settings",
        lambda route: route.fulfill(json={**public_profile, "manager_email": "", "fun_facts": []}),
    )
    page.route(
        "**/api/admin/analytics",
        lambda route: route.fulfill(
            json={
                "total_queries": 1,
                "avg_response_time_ms": 25,
                "queries_by_day": [],
                "confidence_distribution": {"high": 0, "medium": 0, "low": 0, "unknown": 1},
                "recent_queries": [
                    {
                        "query": "Legacy raw question must not be rendered",
                        "confidence_level": "unknown",
                        "response_time_ms": 25,
                        "timestamp": 1_800_000_000_000,
                        "error": "Legacy raw error must not be rendered",
                    }
                ],
                "unique_users": 1,
                "example_question_rate": 0,
                "avg_session_depth": 1,
                "error_count": 1,
            }
        ),
    )
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_role("tab", name="Usage", exact=True).click()
    expect(page.get_by_text(re.compile("Raw questions and visitor identities are not retained"))).to_be_visible()
    page.get_by_role("button", name="Load Analytics", exact=True).click()
    expect(page.get_by_text("Sessions", exact=True)).to_be_visible()
    expect(page.get_by_text("Unique Users", exact=True)).to_have_count(0)
    expect(page.get_by_text("Chat request", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile("Legacy raw"))).to_have_count(0)


def test_owner_sign_in_is_compact_branded_and_busy(page: Page, live_server_url: str, owner_settings):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    expect(page.get_by_role("heading", name="Manage your assistant", exact=True)).to_be_visible()
    expect(page.get_by_role("banner").get_by_role("link", name="Back to assistant", exact=True)).to_be_visible()
    expect(page.get_by_role("contentinfo")).to_have_count(0)
    card = page.locator('[class*="authCard"]')
    box = card.bounding_box()
    assert box is not None and box["width"] <= 500
    assert_text_contrast(button.locator(".ms-Button-label"))
    capture_screenshot(page, "owner-sign-in-desktop")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    capture_screenshot(page, "owner-sign-in-mobile")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate("window.ownerAuthMocks.pauseSignIn = true")
    button.click()
    busy = page.get_by_role("button", name="Opening Microsoft sign-in...", exact=True)
    expect(busy).to_be_disabled()
    assert page.evaluate("window.ownerAuthMocks.signIns") == 1
    page.wait_for_function("typeof window.ownerAuthMocks.finishSignIn === 'function'")
    page.evaluate("window.ownerAuthMocks.finishSignIn()")
    expect(page.get_by_role("heading", name="Owner workspace", exact=True)).to_be_visible()
    expect(page.get_by_text("Signed in as owner@example.test", exact=True)).to_be_visible()


def test_owner_sign_in_cancellation_keeps_retry_available(page: Page, live_server_url: str, owner_settings):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    page.evaluate("window.ownerAuthMocks.cancelSignIn = true")
    button.click()
    expect(page.get_by_text("Sign-in was canceled. You can try again.", exact=True)).to_be_visible()
    expect(button).to_be_enabled()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_count(0)


def test_owner_real_msal_opens_popup_and_recovers_after_cancellation(page: Page, live_server_url: str, real_owner_auth):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    with page.expect_popup() as opened:
        button.click()
    popup = opened.value
    expect(popup.get_by_role("heading", name="Microsoft sign-in test provider", exact=True)).to_be_visible()
    assert urlsplit(popup.url).hostname == "login.microsoftonline.com"
    expect(page.get_by_role("button", name="Opening Microsoft sign-in...", exact=True)).to_be_disabled()
    popup.close()
    expect(page.get_by_text("Sign-in was canceled. You can try again.", exact=True)).to_be_visible()
    expect(button).to_be_enabled()
    with page.expect_popup() as reopened:
        button.click()
    expect(reopened.value.get_by_role("heading", name="Microsoft sign-in test provider", exact=True)).to_be_visible()
    reopened.value.close()
    expect(button).to_be_enabled()


def test_owner_real_msal_reset_recovers_stuck_interaction(page: Page, live_server_url: str, real_owner_auth):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    page.evaluate(
        """clientId => {
            sessionStorage.setItem('msal.interaction.status', JSON.stringify({clientId, type: 'sign_in'}));
            sessionStorage.setItem('unrelated-session-data', 'preserve');
            localStorage.setItem('unrelated-local-data', 'preserve');
        }""",
        real_owner_auth,
    )
    popups = []
    page.on("popup", lambda popup: popups.append(popup))
    button.click()
    expect(
        page.get_by_text(
            "Microsoft sign-in is already active or was interrupted. Close any Microsoft sign-in window, then reset sign-in.",
            exact=True,
        )
    ).to_be_visible()
    assert popups == []
    reset = page.get_by_role("button", name="Reset sign-in", exact=True)
    assert_text_contrast(reset.locator(".ms-Button-label"))
    reset.click()
    expect(button).to_be_enabled()
    assert page.evaluate("sessionStorage.getItem('unrelated-session-data')") == "preserve"
    assert page.evaluate("localStorage.getItem('unrelated-local-data')") == "preserve"
    with page.expect_popup() as opened:
        button.click()
    expect(opened.value.get_by_role("heading", name="Microsoft sign-in test provider", exact=True)).to_be_visible()
    opened.value.close()
    expect(button).to_be_enabled()


def test_owner_real_msal_reports_blocked_popup(page: Page, live_server_url: str, real_owner_auth):
    page.add_init_script("window.open = () => null;")
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(
        page.get_by_text(
            "Your browser could not open Microsoft sign-in. Allow pop-ups for this site and try again.",
            exact=True,
        )
    ).to_be_visible()
    expect(page.get_by_role("button", name="Reset sign-in", exact=True)).to_be_visible()


def test_owner_sign_in_reset_failure_is_visible(page: Page, live_server_url: str, owner_auth):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    page.evaluate("""() => {
        window.ownerAuthMocks.signInErrorCode = 'interaction_in_progress';
        window.ownerAuthMocks.failReset = true;
    }""")
    button.click()
    reset = page.get_by_role("button", name="Reset sign-in", exact=True)
    reset.click()
    expect(
        page.get_by_text(
            "Unable to reset Microsoft sign-in. Check that browser storage is allowed for this site and try again.",
            exact=True,
        )
    ).to_be_visible()
    expect(reset).to_be_enabled()
    assert page.evaluate("window.ownerAuthMocks.resets") == 1


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("state_not_found", "Microsoft sign-in failed (state_not_found). Try again or reset sign-in."),
        ("not a safe diagnostic code", "Microsoft sign-in did not finish. Please try again."),
    ],
)
def test_owner_sign_in_diagnostic_only_displays_safe_codes(page: Page, live_server_url: str, owner_auth, code, message):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    page.evaluate("code => {window.ownerAuthMocks.signInErrorCode = code;}", code)
    button.click()
    expect(page.get_by_text(message, exact=True)).to_be_visible()


def test_owner_auth_setup_failure_is_retryable_not_disabled(page: Page, live_server_url: str):
    state = {"fail": True}
    page.route(
        "**/auth_setup_admin",
        lambda route: (
            route.fulfill(status=503, json={"error": "Internal diagnostic not for display"})
            if state["fail"]
            else route.fulfill(json={"enabled": False})
        ),
    )
    page.goto(f"{live_server_url}#/admin")
    expect(page.get_by_text("Unable to prepare owner sign-in. Please try again.", exact=True)).to_be_visible()
    expect(page.get_by_text("Owner workspace is not configured.", exact=True)).to_have_count(0)
    expect(page.get_by_text("Internal diagnostic not for display", exact=True)).to_have_count(0)
    state["fail"] = False
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_text("Owner workspace is not configured.", exact=True)).to_be_visible()


def test_owner_workspace_draft_preview_and_discard(page: Page, live_server_url: str, public_profile, owner_settings):
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    for name in ("Profile & branding", "Conversation", "Contact & availability"):
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
    preview = page.get_by_role("complementary", name="Visitor preview", exact=True)
    expect(preview.get_by_role("heading", name=public_profile["assistant_name"], exact=True)).to_be_visible()
    expect(page.get_by_label("Private Manager Email", exact=True)).to_have_count(0)
    expect(page.get_by_label("Alternate Contact Email", exact=True)).to_be_visible()
    page.get_by_label("Assistant Name", exact=True).fill("Draft assistant")
    page.get_by_label("Welcome Message", exact=True).fill("A draft welcome.")
    page.get_by_label("Example Questions", exact=True).fill("A draft question?\nAnother draft question?")
    expect(preview.get_by_role("heading", name="Draft assistant", exact=True)).to_be_visible()
    expect(preview.get_by_text("A draft welcome.", exact=True)).to_be_visible()
    expect(preview.get_by_text("Another draft question?", exact=True)).to_be_visible()
    assert page.title() == public_profile["assistant_name"]
    expect(
        page.get_by_role("banner").get_by_role("link", name=public_profile["assistant_name"], exact=True)
    ).to_be_visible()
    expect(page.get_by_text("You have unsaved changes", exact=True)).to_be_visible()
    bar = page.get_by_role("region", name="Save your changes", exact=True)
    box = bar.bounding_box()
    assert box is not None and 0 <= box["y"] < page.viewport_size["height"]
    assert owner_settings["updates"] == []
    unload = """() => {
        const event = new Event("beforeunload", {cancelable: true});
        return window.dispatchEvent(event);
    }"""
    assert page.evaluate(unload) is False
    page.get_by_role("button", name="Discard changes", exact=True).click()
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value(public_profile["assistant_name"])
    expect(page.get_by_label("Welcome Message", exact=True)).to_have_value(public_profile["welcome_message"])
    expect(preview.get_by_role("heading", name=public_profile["assistant_name"], exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Save changes", exact=True)).to_be_disabled()
    assert page.evaluate(unload) is True
    assert owner_settings["updates"] == []


def test_owner_workspace_guards_navigation_without_saving(page: Page, live_server_url: str, owner_settings):
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Assistant Name", exact=True).fill("Unsaved assistant")
    back = page.get_by_role("banner").get_by_role("link", name="Back to assistant", exact=True)
    back.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_have_attribute("aria-modal", "true")
    expect(dialog.get_by_role("heading", name="Leave without saving?", exact=True)).to_be_visible()
    page.get_by_role("button", name="Keep editing", exact=True).click()
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value("Unsaved assistant")
    back.click()
    expect(dialog.get_by_role("heading", name="Leave without saving?", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value("Unsaved assistant")
    expect(page.get_by_role("heading", name="Leave without saving?", exact=True)).to_have_count(0)
    back.click()
    page.get_by_role("button", name="Discard and continue", exact=True).click()
    expect(page.get_by_role("textbox", name="Your message", exact=True)).to_be_visible()
    assert owner_settings["updates"] == []


def test_owner_workspace_guards_sign_out(page: Page, live_server_url: str, owner_settings):
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Assistant Name", exact=True).fill("Unsaved assistant")
    sign_out = page.get_by_role("button", name="Sign out", exact=True)
    sign_out.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_have_attribute("aria-modal", "true")
    expect(dialog.get_by_role("heading", name="Leave without saving?", exact=True)).to_be_visible()
    assert page.evaluate("window.ownerAuthMocks.signOuts") == 0
    page.get_by_role("button", name="Keep editing", exact=True).click()
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value("Unsaved assistant")
    sign_out.click()
    page.get_by_role("button", name="Discard and continue", exact=True).click()
    expect(page.get_by_role("button", name="Continue with Microsoft", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_count(0)
    assert page.evaluate("window.ownerAuthMocks.signOuts") == 1
    assert owner_settings["updates"] == []


def test_owner_preview_rejects_unsafe_image_without_request(
    page: Page, live_server_url: str, owner_settings, browser_requests
):
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Avatar URL", exact=True).fill("javascript:alert('not executed')")
    preview = page.get_by_role("complementary", name="Visitor preview", exact=True)
    expect(
        preview.get_by_text("Enter a site-relative image path or an HTTPS URL to preview this avatar.", exact=True)
    ).to_be_visible()
    assert not any("javascript:" in request["path"] for request in browser_requests)
    page.get_by_role("button", name="Discard changes", exact=True).click()


def test_owner_workspace_mobile_layout(page: Page, live_server_url: str, owner_settings):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(page.get_by_role("heading", name="Owner workspace", exact=True)).to_be_visible()
    expect(page.get_by_role("region", name="Save your changes", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert_text_contrast(page.get_by_label("Owner Name", exact=True))


def test_owner_cached_sign_in_requires_authorized_settings(page: Page, live_server_url: str, owner_settings):
    page.goto(live_server_url)
    page.evaluate("window.ownerAuthMocks.cachedToken = 'browser-owner-id-token'")
    page.get_by_role("banner").get_by_role("link", name="Owner workspace", exact=True).click()
    expect(page.get_by_role("heading", name="Owner workspace", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_value(owner_settings["settings"]["owner_name"])
    assert page.evaluate("window.ownerAuthMocks.signIns") == 0


def test_owner_can_switch_account_after_denial(page: Page, live_server_url: str, public_profile, owner_auth):
    state = {"allowed": False}

    def handle(route: Route):
        if not state["allowed"]:
            route.fulfill(status=403, json={"error": "Owner access required"})
        else:
            route.fulfill(json={**public_profile, "manager_email": "", "fun_facts": []})

    page.route("**/api/admin/settings", handle)
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    expect(page.get_by_role("button", name="Use another Microsoft account", exact=True)).to_be_visible()
    expect(page.get_by_label("Owner Name", exact=True)).to_have_count(0)
    state["allowed"] = True
    page.evaluate("window.ownerAuthMocks.account = {name:'Correct Owner',username:'correct-owner@example.test'}")
    page.get_by_role("button", name="Use another Microsoft account", exact=True).click()
    expect(page.get_by_label("Owner Name", exact=True)).to_be_visible()
    expect(page.get_by_text("Signed in as correct-owner@example.test", exact=True)).to_be_visible()
    assert page.evaluate("window.ownerAuthMocks.signIns") == 2


def test_owner_conflict_reload_requires_discard_and_refreshes_profile(
    page: Page, live_server_url: str, public_profile, owner_auth
):
    state = {"settings": {**public_profile, "manager_email": "", "fun_facts": []}, "revision": 1}

    def handle(route: Route):
        if route.request.method == "PUT":
            state["settings"]["assistant_name"] = "Saved in another session"
            public_profile["assistant_name"] = "Saved in another session"
            state["revision"] = 2
            route.fulfill(status=412, json={"error": "Revision conflict"})
        else:
            route.fulfill(json=state["settings"], headers={"ETag": f'"revision-{state["revision"]}"'})

    page.route("**/api/admin/settings", handle)
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Assistant Name", exact=True).fill("Local draft")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(
        page.get_by_text("Another session changed these settings. Reload the latest version before saving.", exact=True)
    ).to_be_visible()
    reload_button = page.get_by_role("button", name="Reload latest settings", exact=True)
    reload_button.click()
    expect(page.get_by_role("heading", name="Leave without saving?", exact=True)).to_be_visible()
    page.get_by_role("button", name="Keep editing", exact=True).click()
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value("Local draft")
    reload_button.click()
    page.get_by_role("button", name="Discard and continue", exact=True).click()
    expect(page.get_by_label("Assistant Name", exact=True)).to_have_value("Saved in another session")
    expect(page.get_by_role("button", name="Save changes", exact=True)).to_be_disabled()
    expect(page.get_by_role("banner").get_by_role("link", name="Saved in another session", exact=True)).to_be_visible()


def test_owner_navigation_waits_for_inflight_save(page: Page, live_server_url: str, owner_settings):
    pending = []

    def hold_save(route: Route):
        if route.request.method == "PUT":
            pending.append(route)
        else:
            route.fallback()

    page.route("**/api/admin/settings", hold_save)
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Assistant Tone", exact=True).fill("Direct and kind.")
    with page.expect_request(
        lambda request: request.method == "PUT" and urlsplit(request.url).path == "/api/admin/settings"
    ):
        page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_role("button", name="Saving changes...", exact=True)).to_be_disabled()
    page.get_by_role("banner").get_by_role("link", name="Back to assistant", exact=True).click()
    expect(page.get_by_role("heading", name="Saving your changes", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Discard and continue", exact=True)).to_have_count(0)
    assert len(pending) == 1
    owner_settings["settings"].update(pending[0].request.post_data_json)
    pending[0].fulfill(json=owner_settings["settings"], headers={"ETag": '"saved-version-2"'})
    expect(page.get_by_role("textbox", name="Your message", exact=True)).to_be_visible()


def test_owner_save_success_is_distinct_from_public_refresh_failure(page: Page, live_server_url: str, owner_settings):
    state = {"status_calls": 0}

    def status(route: Route):
        state["status_calls"] += 1
        if state["status_calls"] == 1:
            route.fulfill(json=owner_settings["settings"])
        else:
            route.fulfill(status=503, json={"error": "Temporary status failure"})

    page.route("**/api/status", status)
    page.goto(f"{live_server_url}#/admin")
    page.get_by_role("button", name="Continue with Microsoft", exact=True).click()
    page.get_by_label("Assistant Name", exact=True).fill("Saved assistant")
    page.get_by_role("button", name="Save changes", exact=True).click()
    expect(page.get_by_text("Changes saved. Your assistant is up to date.", exact=True)).to_be_visible()
    expect(
        page.get_by_text(
            "Your saved settings are up to date, but the public profile could not refresh. Reload the page to see the latest version.",
            exact=True,
        )
    ).to_be_visible()
    assert owner_settings["updates"] == [{"assistant_name": "Saved assistant"}]
    expect(page.get_by_role("button", name="Save changes", exact=True)).to_be_disabled()


@pytest.mark.parametrize("authenticated", [False, True])
def test_owner_workspace_accessibility(page: Page, live_server_url: str, owner_settings, authenticated):
    page.goto(f"{live_server_url}#/admin")
    button = page.get_by_role("button", name="Continue with Microsoft", exact=True)
    expect(button).to_be_visible()
    if authenticated:
        button.click()
        expect(page.get_by_label("Owner Name", exact=True)).to_be_visible()
    results = Axe().run(page)
    assert results.violations_count == 0, results.generate_report()


def test_streaming_preserves_multiturn_messages_and_session(page: Page, live_server_url: str):
    session = {"conversation": "browser-regression", "turn": 1}
    second_answer = "The next project added evaluation and monitoring."
    captured = mock_chat(
        page, [chat_response(session_state=session), chat_response(second_answer, session_state=session)]
    )
    page.goto(live_server_url)
    send_question(page)
    expect_app_text(page, ANSWER)
    expect(page.get_by_role("textbox")).to_have_value("")
    send_question(page, "What did you do next?")
    expect_app_text(page, second_answer)
    expect(page.get_by_text(ANSWER, exact=True)).to_be_visible()
    assert len(captured) == 2
    assert [request["path"] for request in captured] == ["/chat/stream", "/chat/stream"]
    assert all(request["method"] == "POST" and request["content_type"] == "application/json" for request in captured)
    assert captured[0]["body"]["session_state"] is None
    assert captured[0]["body"]["messages"] == [{"role": "user", "content": QUESTION}]
    assert captured[1]["body"]["session_state"] == session
    assert captured[1]["body"]["messages"] == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "What did you do next?"},
    ]
    overrides = captured[0]["body"]["context"]["overrides"]
    assert overrides["temperature"] == 0.3
    assert overrides["suggest_followup_questions"] is False
    assert overrides["language"] in ("en", "en-US")
    assert not {"retrieval_mode", "minimum_search_score", "confidence_threshold"}.intersection(overrides)


def test_nonstreaming_uses_chat_endpoint(page: Page, live_server_url: str, api_config):
    api_config["streamingEnabled"] = False
    captured = mock_chat(page, [chat_response(context={"confidence_level": "high"})])
    page.goto(live_server_url)
    send_question(page)
    expect_app_text(page, ANSWER)
    assert len(captured) == 1
    assert captured[0]["path"] == "/chat"
    assert captured[0]["method"] == "POST"
    assert captured[0]["body"]["session_state"] is None
    expect(page.get_by_role("button", name=re.compile("Speak answer"))).to_have_count(0)
    expect(page.get_by_text(re.compile(r"\bconfidence\b", re.IGNORECASE))).to_have_count(0)
    page.get_by_role("button", name="Clear chat").click()
    expect(page.get_by_text(ANSWER, exact=True)).to_have_count(0)
    expect(page.get_by_text(QUESTION, exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Clear chat")).to_be_disabled()


def test_config_failure_blocks_send_until_retry_succeeds(page: Page, live_server_url: str, api_config):
    pending_config = []
    captured = mock_chat(page, [chat_response()])

    def handle_config(route: Route):
        pending_config.append(route)
        if len(pending_config) > 1:
            route.fulfill(json=api_config)

    page.route("**/config", handle_config)
    page.goto(live_server_url)
    expect(page.get_by_text("Loading chat configuration", exact=True)).to_be_visible()
    page.get_by_role("textbox").fill(QUESTION)
    page.get_by_role("textbox").press("Enter")
    expect(page.get_by_role("button", name="Submit question")).to_be_disabled()
    assert captured == []
    assert len(pending_config) == 1
    pending_config[0].fulfill(status=503, json={"error": "Configuration unavailable"})
    expect(page.get_by_role("alert")).to_contain_text("Unable to load chat configuration. Please retry.")
    expect(page.get_by_role("button", name="Submit question")).to_be_disabled()
    page.get_by_role("textbox").press("Enter")
    assert captured == []
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(page.get_by_role("button", name="Submit question")).to_be_enabled()
    assert len(pending_config) == 2
    assert captured == []
    page.get_by_role("button", name="Submit question").click()
    expect_app_text(page, ANSWER)
    assert len(captured) == 1


@pytest.mark.parametrize("streaming", [True, False], ids=["stream-error", "http-error"])
def test_chat_failure_can_retry(page: Page, live_server_url: str, api_config, streaming: bool):
    api_config["streamingEnabled"] = streaming
    captured = []

    def handle(route: Route):
        captured.append(route.request.post_data_json)
        if len(captured) == 1:
            if streaming:
                route.fulfill(
                    content_type="application/x-ndjson", body=json.dumps({"error": "Mock stream failed"}) + "\n"
                )
            else:
                route.fulfill(status=503, json={"error": "Mock service unavailable"})
        elif streaming:
            route.fulfill(content_type="application/x-ndjson", body=stream_body(chat_response()))
        else:
            route.fulfill(json=chat_response())

    page.route(re.compile(r"/chat(?:/stream)?$"), handle)
    page.goto(live_server_url)
    send_question(page)
    expect_app_text(page, re.compile("Mock stream failed" if streaming else "Mock service unavailable"))
    page.get_by_role("button", name="Retry", exact=True).click()
    expect_app_text(page, ANSWER)
    expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    assert len(captured) == 2
    assert captured[0] == captured[1]


@pytest.mark.parametrize("streaming", [True, False])
def test_context_trimming_notice_preserves_client_history(page: Page, live_server_url: str, api_config, streaming):
    api_config["streamingEnabled"] = streaming
    responses = [chat_response(), chat_response("A grounded follow-up.", context={"history_truncated": True})]
    captured = mock_chat(page, responses)
    page.goto(live_server_url)
    send_question(page)
    expect_app_text(page, ANSWER)
    send_question(page, "Tell me more.")
    expect_app_text(page, "A grounded follow-up.")
    expect(page.get_by_role("status")).to_have_text(
        "Earlier turns were omitted from this response to fit the model's context window. Your saved history is unchanged."
    )
    assert captured[1]["body"]["messages"] == [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "Tell me more."},
    ]


def test_markdown_renderer_loads_only_after_an_answer(page: Page, live_server_url: str):
    mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    expect(page.get_by_role("textbox")).to_be_enabled()
    assert (
        page.evaluate(
            "performance.getEntriesByType('resource').some(resource => resource.name.includes('/AnswerContent-'))"
        )
        is False
    )
    send_question(page)
    expect_app_text(page, ANSWER)
    assert (
        page.evaluate(
            "performance.getEntriesByType('resource').some(resource => resource.name.includes('/AnswerContent-'))"
        )
        is True
    )


def test_clear_pending_stream_cancels_stale_answer(page: Page, live_server_url: str, api_config):
    api_config["showChatHistoryBrowser"] = True
    page.add_init_script("""(() => {
            const originalFetch = window.fetch.bind(window);
            window.pendingChat = {requests: []};
            window.fetch = (input, options) => {
                if (new URL(input, location.href).pathname !== '/chat/stream' || window.pendingChat.requests.length) {
                    return originalFetch(input, options);
                }
                window.pendingChat.requests.push(JSON.parse(options.body));
                window.pendingChat.signal = options.signal;
                const stream = new ReadableStream({start(controller) {window.pendingChat.controller = controller;}});
                return Promise.resolve(new Response(stream, {headers: {'Content-Type': 'application/x-ndjson'}}));
            };
            window.pushChatEvent = event => window.pendingChat.controller.enqueue(
                new TextEncoder().encode(JSON.stringify(event) + '\\n')
            );
        })();""")
    captured = mock_chat(page, [chat_response("Fresh conversation answer", session_state="fresh-session")])
    page.goto(live_server_url)
    send_question(page)
    page.wait_for_function("window.pendingChat.controller !== undefined")
    page.evaluate(
        """response => {
            window.pushChatEvent({context: response.context, session_state: response.session_state, delta: {role: 'assistant'}});
            window.pushChatEvent({delta: {content: 'Partial answer'}});
        }""",
        chat_response(),
    )
    expect_app_text(page, "Partial answer")
    expect(page.get_by_role("button", name="Open chat history")).to_be_disabled()
    expect(page.get_by_role("button", name="Submit question")).to_be_disabled()
    page.get_by_role("button", name="Clear chat").click()
    assert page.evaluate("window.pendingChat.signal.aborted") is True
    expect(page.get_by_text("Partial answer", exact=True)).to_have_count(0)
    send_question(page, "Start a fresh conversation")
    page.evaluate("""() => {
            window.pushChatEvent({delta: {content: ' stale cancelled content'}});
            window.pendingChat.controller.close();
        }""")
    expect_app_text(page, "Fresh conversation answer")
    expect(page.get_by_text(re.compile("Partial answer|stale cancelled content"))).to_have_count(0)
    expect(page.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    assert len(captured) == 1
    assert captured[0]["body"]["session_state"] is None
    assert captured[0]["body"]["messages"] == [{"role": "user", "content": "Start a fresh conversation"}]


def seed_history(page: Page, count: int, response=None, question=None) -> None:
    items = [
        {
            "id": f"history-{index:02}",
            "title": f"Saved conversation {index:02}",
            "timestamp": 1_800_000_000_000 - index * 1000,
            "answers": [
                [
                    question or f"Saved question {index:02}",
                    response or chat_response(f"Saved answer {index:02}", f"history-{index:02}"),
                ]
            ],
        }
        for index in range(count)
    ]
    page.evaluate(
        """items => new Promise((resolve, reject) => {
            const opening = indexedDB.open('chat-database', 1);
            opening.onerror = () => reject(opening.error);
            opening.onupgradeneeded = () => {
                const store = opening.result.createObjectStore('chat-history', {keyPath: 'id'});
                store.createIndex('timestamp', 'timestamp');
            };
            opening.onsuccess = () => {
                const database = opening.result;
                const transaction = database.transaction('chat-history', 'readwrite');
                transaction.oncomplete = () => {database.close(); resolve();};
                transaction.onerror = () => {database.close(); reject(transaction.error);};
                const store = transaction.objectStore('chat-history');
                store.clear();
                items.forEach(item => store.put(item));
            };
        })""",
        items,
    )


def test_history_empty_panel_opens_and_closes(page: Page, live_server_url: str, api_config):
    api_config["showChatHistoryBrowser"] = True
    page.goto(live_server_url)
    page.get_by_role("button", name="Open chat history").click()
    expect(page.get_by_role("heading", name="Chat history", exact=True)).to_be_visible()
    expect(page.get_by_text("No chat history", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Load more", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close chat history", exact=True).click()
    expect(page.get_by_role("heading", name="Chat history", exact=True)).to_have_count(0)


def test_anonymous_history_saves_locally_and_survives_reload(
    page: Page, live_server_url: str, api_config, browser_requests
):
    api_config.update(showChatHistoryBrowser=True, showChatHistoryCosmos=True)
    mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    expect(page.get_by_text(re.compile("Chat history is saved on this browser"))).to_be_visible()
    send_question(page)
    expect_app_text(page, ANSWER)
    page.get_by_role("button", name="Open chat history", exact=True).click()
    expect(page.get_by_role("button", name=QUESTION, exact=True)).to_be_visible()
    page.reload()
    page.get_by_role("button", name="Open chat history", exact=True).click()
    page.get_by_role("button", name=QUESTION, exact=True).click()
    expect_app_text(page, ANSWER)
    expect(page.get_by_text(QUESTION, exact=True)).to_be_visible()
    assert not any(request["path"].startswith("/chat_history") for request in browser_requests)


def test_history_loading_pagination_and_reset(page: Page, live_server_url: str, api_config):
    api_config["showChatHistoryBrowser"] = True
    page.goto(live_server_url)
    seed_history(page, 23)
    page.evaluate("""() => new Promise((resolve, reject) => {
            const opening = indexedDB.open('chat-database', 1);
            opening.onerror = () => reject(opening.error);
            opening.onsuccess = () => {
                const database = opening.result;
                const transaction = database.transaction('chat-history', 'readwrite');
                const store = transaction.objectStore('chat-history');
                let keepWaiting = true;
                const completed = new Promise(done => {transaction.oncomplete = () => {database.close(); done();};});
                window.releaseHistoryRead = () => {keepWaiting = false; return completed;};
                const keepAlive = () => {
                    const request = store.get('history-00');
                    request.onsuccess = () => {resolve(); if (keepWaiting) keepAlive();};
                };
                keepAlive();
            };
        })""")
    page.get_by_role("button", name="Open chat history").click()
    expect(page.locator(".ms-Panel .ms-Spinner")).to_be_visible()
    expect(page.get_by_text("No chat history", exact=True)).to_have_count(0)
    page.evaluate("window.releaseHistoryRead()")
    items = page.get_by_role("button", name=re.compile(r"^Saved conversation \d{2}$"))
    expect(items).to_have_count(20)
    expect(page.locator(".ms-Panel .ms-Spinner")).to_have_count(0)
    expect(page.get_by_role("button", name="Saved conversation 20", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Load more", exact=True).click()
    expect(items).to_have_count(23)
    assert items.all_text_contents() == [f"Saved conversation {index:02}" for index in range(23)]
    page.get_by_role("button", name="Load more", exact=True).click()
    expect(page.get_by_role("button", name="Load more", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close chat history", exact=True).click()
    page.get_by_role("button", name="Open chat history").click()
    expect(items).to_have_count(20)


def test_history_selection_restores_messages_and_session(page: Page, live_server_url: str, api_config):
    api_config["showChatHistoryBrowser"] = True
    captured = mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    seed_history(page, 1)
    page.get_by_role("button", name="Open chat history").click()
    page.get_by_role("button", name="Saved conversation 00", exact=True).click()
    expect(page.get_by_text("Saved question 00", exact=True)).to_be_visible()
    expect(page.get_by_text("Saved answer 00", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Chat history", exact=True)).to_have_count(0)
    send_question(page)
    expect_app_text(page, ANSWER)
    assert captured[0]["body"]["session_state"] == "history-00"
    assert captured[0]["body"]["messages"] == [
        {"role": "user", "content": "Saved question 00"},
        {"role": "assistant", "content": "Saved answer 00"},
        {"role": "user", "content": QUESTION},
    ]


def test_history_delete_requires_confirmation_and_persists(page: Page, live_server_url: str, api_config):
    api_config["showChatHistoryBrowser"] = True
    page.goto(live_server_url)
    seed_history(page, 1)
    page.get_by_role("button", name="Open chat history").click()
    page.get_by_role("button", name="delete this chat history", exact=True).click()
    expect(page.get_by_role("heading", name="Delete chat history", exact=True)).to_be_visible()
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("button", name="Saved conversation 00", exact=True)).to_be_visible()
    page.get_by_role("button", name="delete this chat history", exact=True).click()
    page.get_by_role("button", name="Delete", exact=True).click()
    expect(page.get_by_text("No chat history", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Saved conversation 00", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close chat history", exact=True).click()
    page.get_by_role("button", name="Open chat history").click()
    expect(page.get_by_text("No chat history", exact=True)).to_be_visible()


def open_saved_answer(page: Page, live_server_url: str, api_config, response=None) -> None:
    api_config["showChatHistoryBrowser"] = True
    page.goto(live_server_url)
    seed_history(page, 1, response)
    page.get_by_role("button", name="Open chat history").click()
    page.get_by_role("button", name="Saved conversation 00", exact=True).click()
    expect(page.get_by_text("Saved question 00", exact=True)).to_be_visible()


def test_legacy_speech_flags_do_not_restore_redundant_controls(page: Page, live_server_url: str, api_config):
    api_config.update(showSpeechInput=True, showSpeechOutputAzure=True, showSpeechOutputBrowser=True)
    mock_chat(page, [chat_response()])
    page.goto(live_server_url)
    expect(page.get_by_role("button", name="Ask question with voice", exact=True)).to_have_count(0)
    send_question(page)
    expect_app_text(page, ANSWER)
    expect(page.get_by_role("button", name=re.compile("Speak answer"))).to_have_count(0)


@pytest.fixture
def realtime_voice(page: Page, api_config):
    api_config["showRealtimeVoice"] = True
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""(() => {
        const mocks = window.voiceMocks = {
            sockets: [], requests: [], tracks: [], contexts: [], worklets: [],
            moduleUrls: [], revokedUrls: [], pendingModules: [],
            deferPermission: false, deferModule: false, failModule: false, failSocket: false
        };
        Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: () => {
            return new Promise((resolve, reject) => {
                const request = {
                    resolve: () => {
                        const track = {readyState: 'live', stop() {this.readyState = 'ended';}};
                        mocks.tracks.push(track);
                        resolve({getTracks: () => [track]});
                    },
                    reject: () => reject(new DOMException('Microphone permission denied', 'NotAllowedError'))
                };
                mocks.requests.push(request);
                if (!mocks.deferPermission) request.resolve();
            });
        }});
        const revokeObjectURL = URL.revokeObjectURL.bind(URL);
        URL.revokeObjectURL = url => {mocks.revokedUrls.push(url); revokeObjectURL(url);};
        window.AudioContext = class {
            constructor() {
                this.state = 'running';
                this.currentTime = 0;
                this.destination = {};
                this.audioWorklet = {addModule: url => {
                    mocks.moduleUrls.push(url);
                    if (mocks.failModule) return Promise.reject(new Error('Mock worklet initialization failed'));
                    if (mocks.deferModule) return new Promise(resolve => mocks.pendingModules.push(resolve));
                    return Promise.resolve();
                }};
                mocks.contexts.push(this);
            }
            close() {this.state = 'closed'; return Promise.resolve();}
            createMediaStreamSource() {return {connect() {}, disconnect() {}};}
            createBuffer(channels, length, sampleRate) {
                return {duration: length / sampleRate, getChannelData: () => new Float32Array(length)};
            }
            createBufferSource() {return {connect() {}, start() {}};}
        };
        window.AudioWorkletNode = class {
            constructor() {
                this.port = {onmessage: null, close() {}};
                mocks.worklets.push(this);
            }
            connect() {}
            disconnect() {}
        };
        window.WebSocket = class {
            static CONNECTING = 0;
            static OPEN = 1;
            static CLOSING = 2;
            static CLOSED = 3;
            constructor(url) {
                if (mocks.failSocket) throw new Error('Mock WebSocket constructor failed');
                this.url = url;
                this.readyState = 0;
                this.closeCalls = 0;
                this.sent = [];
                mocks.sockets.push(this);
            }
            open() {this.readyState = 1; this.onopen?.(new Event('open'));}
            close() {this.closeCalls += 1; this.readyState = 2;}
            finishClose(code, reason) {
                this.readyState = 3;
                this.onclose?.({code, reason, wasClean: code === 1000});
            }
            fail() {this.onerror?.(new Event('error'));}
            message(data) {this.onmessage?.({data: JSON.stringify(data)});}
            send(data) {this.sent.push(JSON.parse(data));}
        };
    })();""")
    yield
    assert not errors, f"Unhandled voice browser errors: {errors}"


def start_voice_connection(page: Page, socket_index: int = 0) -> None:
    page.get_by_role("button", name="Start Voice Chat", exact=True).click()
    page.wait_for_function("index => window.voiceMocks.sockets.length > index", arg=socket_index)
    page.evaluate("index => window.voiceMocks.sockets[index].open()", socket_index)


def expect_voice_audio_released(page: Page) -> None:
    page.wait_for_function("""() =>
        window.voiceMocks.tracks.every(track => track.readyState === 'ended') &&
        window.voiceMocks.contexts.every(context => context.state === 'closed')
    """)


def test_anonymous_voice_entry_waits_for_explicit_action(
    page: Page, live_server_url: str, api_config, realtime_voice, browser_requests
):
    for legacy_flag in (
        "showSpeechInput",
        "showSpeechOutputAzure",
        "showSpeechOutputBrowser",
        "showChatHistoryCosmos",
    ):
        api_config.pop(legacy_flag, None)
    page.goto(live_server_url)
    page.get_by_role("button", name="Try voice chat", exact=True).click()
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_enabled()
    expect(page.get_by_text(re.compile("You are speaking with an AI, not the owner"))).to_be_visible()
    expect(
        page.get_by_text(
            "Your speech is transcribed and shown alongside AI reply text when available. "
            "Transcriptions may contain recognition mistakes.",
            exact=True,
        )
    ).to_be_visible()
    assert page.evaluate("window.voiceMocks.sockets.length") == 0
    assert page.evaluate("window.voiceMocks.requests.length") == 0
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    assert urlsplit(page.evaluate("window.voiceMocks.sockets[0].url")).path == "/realtime"
    assert urlsplit(page.evaluate("window.voiceMocks.sockets[0].url")).query == ""
    page.evaluate("""() => {
        window.voiceMocks.worklets[0].port.onmessage({data: 'AAA='});
        window.voiceMocks.sockets[0].message({type: 'response.audio.delta', delta: 'AAA='});
        window.voiceMocks.sockets[0].message({type: 'response.audio_transcript.delta', delta: 'Hello from the AI assistant.'});
    }""")
    expect(page.get_by_text("Hello from the AI assistant.", exact=True)).to_be_visible()
    expect(page.locator('[class*="userBubble"]')).to_have_count(0)
    assert page.evaluate("window.voiceMocks.sockets[0].sent") == [
        {"type": "input_audio_buffer.append", "audio": "AAA="}
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)
    assert not any(
        request["path"].startswith(("/auth_setup", "/.auth/")) or request["authorization"]
        for request in browser_requests
    )


def test_realtime_voice_markers_dedupe_and_fallback_reset_per_session(page: Page, live_server_url: str, realtime_voice):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.speech_stopped'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'voice-item-1'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'voice-item-1'});
        socket.message({type: 'input_audio_buffer.speech_stopped'});
        for (const item_id of [undefined, null, '', '   ', 42, 'x'.repeat(257)]) {
            socket.message({type: 'input_audio_buffer.committed', item_id});
            socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id, delta: 'Invalid uncorrelated partial'});
            socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id, transcript: 'Invalid uncorrelated transcript'});
        }
    }""")
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(1)
    expect(page.get_by_text("Preparing AI reply...", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio.delta', delta: 'AAA='});
        socket.message({type: 'response.audio_transcript.delta', delta: 'The assistant still replies with audio and text.'});
        socket.message({type: 'response.audio.done'});
    }""")
    expect(page.get_by_text("The assistant still replies with audio and text.", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile("Invalid uncorrelated"))).to_have_count(0)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    assert page.evaluate("window.voiceMocks.contexts.length") == 2

    page.evaluate(
        "window.voiceMocks.sockets[0].message({type: 'input_audio_buffer.committed', item_id: 'voice-item-1'})"
    )
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(1)
    expect(page.get_by_text("Preparing AI reply...", exact=True)).to_have_count(0)
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'voice-item-2'});
        socket.message({type: 'input_audio_buffer.speech_stopped'});
        socket.message({type: 'input_audio_buffer.speech_stopped'});
    }""")
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(2)
    expect(page.get_by_text("The assistant still replies with audio and text.", exact=True)).to_have_count(1)
    assert page.locator('[class*="transcriptArea"] > div').all_text_contents()[:3] == [
        "Voice message",
        "The assistant still replies with audio and text.",
        "Voice message",
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)

    start_voice_connection(page, 1)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(0)
    page.evaluate("""() => {
        window.voiceMocks.sockets[0].message({type: 'input_audio_buffer.committed', item_id: 'stale-item'});
        window.voiceMocks.sockets[0].message({type: 'input_audio_buffer.speech_stopped'});
        window.voiceMocks.sockets[0].message({
            type: 'conversation.item.input_audio_transcription.completed', item_id: 'voice-item-1', transcript: 'Stale words'
        });
    }""")
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(0)
    expect(page.get_by_text("Stale words", exact=True)).to_have_count(0)
    expect(page.get_by_text("Preparing AI reply...", exact=True)).to_have_count(0)
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[1];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'voice-item-1'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'voice-item-1'});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'A fresh session also has an assistant reply.'});
    }""")
    expect(page.get_by_text("Voice message", exact=True)).to_have_count(1)
    expect(page.get_by_text("A fresh session also has an assistant reply.", exact=True)).to_be_visible()
    expect(page.get_by_text("The assistant still replies with audio and text.", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


def test_realtime_voice_updates_correlated_partial_and_final_transcripts(
    page: Page, live_server_url: str, realtime_voice, browser_requests
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'spoken-turn'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'spoken-turn'});
    }""")
    user_bubbles = page.locator('[class*="userBubble"]')
    expect(user_bubbles).to_have_text(["Voice message"])
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'spoken-turn', delta: 'Tell me about'});
    }""")
    expect(user_bubbles).to_have_text(["Tell me about"])
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'spoken-turn', delta: ' your latest project.'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'spoken-turn', delta: 42});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'spoken-turn', transcript: null});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'My latest project is a reliable assistant.'});
    }""")
    expect(user_bubbles).to_have_text(["Tell me about your latest project."])
    expect(page.locator('[class*="assistantBubble"]')).to_have_text(["My latest project is a reliable assistant."])
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'spoken-turn', transcript: 'Tell me about your recent project.'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'spoken-turn', transcript: 'Tell me about your recent project.'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'spoken-turn', transcript: 'Duplicate final must not replace real words.'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'spoken-turn', delta: 'Obsolete partial'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'spoken-turn'});
        window.voiceMocks.worklets[0].port.onmessage({data: 'AAA='});
    }""")
    expect(user_bubbles).to_have_text(["Tell me about your recent project."])
    expect(page.locator('[class*="assistantBubble"]')).to_have_text(["My latest project is a reliable assistant."])
    expect(page.get_by_text("Preparing AI reply...", exact=True)).to_have_count(0)
    assert page.locator('[class*="transcriptArea"] > div').all_text_contents()[:2] == [
        "Tell me about your recent project.",
        "My latest project is a reliable assistant.",
    ]
    assert page.evaluate("window.voiceMocks.sockets[0].sent") == [
        {"type": "input_audio_buffer.append", "audio": "AAA="}
    ]
    assert not any(request["path"].startswith("/chat_history") for request in browser_requests)
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)
    expect(user_bubbles).to_have_text(["Tell me about your recent project."])
    page.reload()
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_enabled()
    expect(user_bubbles).to_have_count(0)
    expect(page.locator('[class*="assistantBubble"]')).to_have_count(0)


def test_realtime_voice_buffers_transcription_before_commit_and_saves_assistant_once(
    page: Page, live_server_url: str, realtime_voice
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'early-turn', delta: 'Early partial'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'early-turn', transcript: 'A transcript that arrived before commit.'});
    }""")
    expect(page.locator('[class*="userBubble"]')).to_have_count(0)
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'early-turn'});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'The first assistant reply.'});
    }""")
    expect(page.locator('[class*="userBubble"]')).to_have_text(["A transcript that arrived before commit."])
    expect(page.get_by_text("The first assistant reply.", exact=True)).to_have_count(1)
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'response.created'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'second-early-turn', transcript: 'The next early transcript.'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'second-early-turn'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'second-early-turn'});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'The second assistant reply.'});
    }""")
    expect(page.locator('[class*="assistantBubble"]')).to_have_text(
        ["The first assistant reply.", "The second assistant reply."]
    )
    assert page.locator('[class*="transcriptArea"] > div').all_text_contents()[:4] == [
        "A transcript that arrived before commit.",
        "The first assistant reply.",
        "The next early transcript.",
        "The second assistant reply.",
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


def test_realtime_voice_correlates_two_out_of_order_transcripts_after_assistant_starts(
    page: Page, live_server_url: str, realtime_voice
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'first-turn'});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'First assistant reply.'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'first-turn', delta: 'First'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'x'.repeat(256)});
        socket.message({type: 'response.created'});
        socket.message({type: 'response.audio_transcript.delta', delta: 'Second assistant reply.'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'x'.repeat(256), delta: 'Second partial'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'x'.repeat(256), transcript: 'Second spoken question.'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'first-turn', delta: ' question partial'});
    }""")
    expect(page.locator('[class*="userBubble"]')).to_have_text(["First question partial", "Second spoken question."])
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'first-turn', transcript: 'First spoken question.'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'first-turn'});
        socket.message({type: 'input_audio_buffer.committed', item_id: 'x'.repeat(256)});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'first-turn', transcript: 'First spoken question.'});
        socket.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'x'.repeat(256), transcript: 'Second spoken question.'});
    }""")
    expect(page.locator('[class*="userBubble"]')).to_have_text(["First spoken question.", "Second spoken question."])
    expect(page.locator('[class*="assistantBubble"]')).to_have_text(
        ["First assistant reply.", "Second assistant reply."]
    )
    assert page.locator('[class*="transcriptArea"] > div').all_text_contents()[:4] == [
        "First spoken question.",
        "First assistant reply.",
        "Second spoken question.",
        "Second assistant reply.",
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


@pytest.mark.parametrize("outcome", ["empty", "failed"])
def test_realtime_voice_empty_or_failed_transcription_keeps_generic_fallback(
    page: Page, live_server_url: str, realtime_voice, outcome: str
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const socket = window.voiceMocks.sockets[0];
        socket.message({type: 'input_audio_buffer.committed', item_id: 'fallback-turn'});
        socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'fallback-turn', delta: 'An unfinished partial'});
    }""")
    expect(page.locator('[class*="userBubble"]')).to_have_text(["An unfinished partial"])
    page.evaluate(
        """outcome => {
            const socket = window.voiceMocks.sockets[0];
            socket.message({
                type: `conversation.item.input_audio_transcription.${outcome === 'empty' ? 'completed' : 'failed'}`,
                item_id: 'fallback-turn', transcript: '   ', error: {message: 'Private transcription diagnostic'}
            });
            socket.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'fallback-turn', delta: 'Obsolete partial'});
            socket.message({type: 'response.created'});
            socket.message({type: 'response.audio_transcript.delta', delta: 'An assistant reply without an input transcript.'});
        }""",
        outcome,
    )
    expect(page.locator('[class*="userBubble"]')).to_have_text(["Voice message"])
    expect(page.locator('[class*="assistantBubble"]')).to_have_text(["An assistant reply without an input transcript."])
    expect(page.get_by_text(re.compile("unfinished|Obsolete|Private transcription"))).to_have_count(0)
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


@pytest.mark.parametrize("config_failure", [False, True], ids=["disabled", "config-error"])
def test_voice_capability_blocks_microphone_when_unavailable(
    page: Page, live_server_url: str, api_config, realtime_voice, config_failure: bool
):
    api_config["showRealtimeVoice"] = False
    if config_failure:
        page.route("**/config", lambda route: route.fulfill(status=503, json={"error": "Configuration unavailable"}))
    page.goto(f"{live_server_url}#/voice")
    expect(page.get_by_text("Voice chat is not enabled for this deployment.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_disabled()
    if config_failure:
        expect(page.get_by_role("alert")).to_contain_text("Unable to load chat configuration")
    assert page.evaluate("window.voiceMocks.sockets.length") == 0
    assert page.evaluate("window.voiceMocks.requests.length") == 0
    page.get_by_role("button", name="Switch to Text Chat", exact=True).click()
    expect(page.get_by_role("textbox", name="Your message", exact=True)).to_be_visible()


@pytest.mark.parametrize("action", ["stop", "navigate", "upstream-failure"])
def test_realtime_voice_cancels_delayed_permission_and_restarts(
    page: Page, live_server_url: str, realtime_voice, action: str
):
    page.goto(f"{live_server_url}#/voice")
    page.evaluate("window.voiceMocks.deferPermission = true")
    start_voice_connection(page)
    page.wait_for_function("window.voiceMocks.requests.length === 1")
    if action == "stop":
        page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    elif action == "navigate":
        page.get_by_role("button", name="Switch to Text Chat", exact=True).click()
        expect(page.get_by_role("textbox")).to_be_visible()
        page.evaluate("window.location.hash = '#/voice'")
    else:
        page.evaluate("window.voiceMocks.sockets[0].finishClose(1011, 'Realtime service unavailable')")
        expect(page.get_by_role("alert")).to_contain_text("Realtime service unavailable")

    start_voice_connection(page, 1)
    page.wait_for_function("window.voiceMocks.requests.length === 2")
    page.evaluate("window.voiceMocks.requests[1].resolve()")
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("window.voiceMocks.requests[0].resolve()")
    page.wait_for_function(
        "window.voiceMocks.tracks.length === 2 && window.voiceMocks.tracks[1].readyState === 'ended'"
    )
    assert page.evaluate("window.voiceMocks.tracks[0].readyState") == "live"
    assert page.evaluate("window.voiceMocks.contexts.length") == 1
    page.evaluate("window.voiceMocks.worklets[0].port.onmessage({data: 'AAA='})")
    assert page.evaluate("window.voiceMocks.sockets[1].sent") == [
        {"type": "input_audio_buffer.append", "audio": "AAA="}
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


@pytest.mark.parametrize("failure", ["permission", "worklet"])
def test_realtime_voice_initialization_failure_releases_microphone_and_restarts(
    page: Page, live_server_url: str, realtime_voice, failure: str
):
    page.goto(f"{live_server_url}#/voice")
    page.evaluate(
        """failure => {
        window.voiceMocks.deferPermission = failure === 'permission';
        window.voiceMocks.failModule = failure === 'worklet';
    }""",
        failure,
    )
    start_voice_connection(page)
    if failure == "permission":
        page.wait_for_function("window.voiceMocks.requests.length === 1")
        page.evaluate("window.voiceMocks.requests[0].reject()")
    expect(page.get_by_role("alert")).to_contain_text("Unable to start microphone")
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    expect_voice_audio_released(page)
    assert page.evaluate("window.voiceMocks.moduleUrls") == page.evaluate("window.voiceMocks.revokedUrls")
    assert page.evaluate("window.voiceMocks.sockets[0].closeCalls") == 1

    page.evaluate("window.voiceMocks.failModule = window.voiceMocks.deferPermission = false")
    start_voice_connection(page, 1)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


def test_realtime_voice_cancels_pending_worklet_initialization(page: Page, live_server_url: str, realtime_voice):
    page.goto(f"{live_server_url}#/voice")
    page.evaluate("window.voiceMocks.deferModule = true")
    start_voice_connection(page)
    page.wait_for_function("window.voiceMocks.pendingModules.length === 1")
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)
    page.evaluate("window.voiceMocks.pendingModules[0]()")
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    page.wait_for_function("window.voiceMocks.revokedUrls.length === 1")
    assert page.evaluate("window.voiceMocks.worklets.length") == 0
    expect(page.get_by_role("alert")).to_have_count(0)


def test_realtime_voice_ignores_stale_socket_callbacks_and_rapid_toggles(
    page: Page, live_server_url: str, realtime_voice
):
    page.goto(f"{live_server_url}#/voice")
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Start Voice Chat", exact=True).evaluate(
        "button => {button.click(); button.click();}"
    )
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    assert page.evaluate("window.voiceMocks.sockets.length") == 1
    assert page.evaluate("window.voiceMocks.requests.length") == 0

    start_voice_connection(page, 1)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("""() => {
        const old = window.voiceMocks.sockets[0];
        old.open();
        old.fail();
        old.message({type: 'response.audio_transcript.delta', delta: 'Stale transcript'});
        old.message({type: 'input_audio_buffer.committed', item_id: 'stale-turn'});
        old.message({type: 'conversation.item.input_audio_transcription.delta', item_id: 'stale-turn', delta: 'Stale partial'});
        old.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'stale-turn', transcript: 'Stale user'});
        old.message({type: 'response.audio.delta', delta: 'AAA='});
        old.finishClose(1011, 'Stale failure');
    }""")
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile("Stale"))).to_have_count(0)
    expect(page.get_by_role("alert")).to_have_count(0)
    assert page.evaluate("window.voiceMocks.contexts.length") == 1
    page.evaluate("window.voiceMocks.worklets[0].port.onmessage({data: 'AAA='})")
    assert page.evaluate("window.voiceMocks.sockets[0].sent") == []
    assert page.evaluate("window.voiceMocks.sockets[1].sent") == [
        {"type": "input_audio_buffer.append", "audio": "AAA="}
    ]
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


@pytest.mark.parametrize("failure", ["constructor", "error", "close"])
def test_realtime_voice_connection_failure_does_not_request_microphone(
    page: Page, live_server_url: str, realtime_voice, failure: str
):
    page.goto(f"{live_server_url}#/voice")
    page.evaluate("failure => window.voiceMocks.failSocket = failure === 'constructor'", failure)
    page.get_by_role("button", name="Start Voice Chat", exact=True).click()
    if failure == "error":
        page.evaluate("window.voiceMocks.sockets[0].fail()")
        expect(page.get_by_role("alert")).to_contain_text("Unable to connect")
        page.evaluate("window.voiceMocks.sockets[0].finishClose(1011, 'Realtime service unavailable')")
    elif failure == "close":
        page.evaluate("window.voiceMocks.sockets[0].finishClose(1008, 'Authentication failed')")
    expected = {
        "constructor": "Unable to connect",
        "error": "Realtime service unavailable",
        "close": "Authentication failed",
    }
    expect(page.get_by_role("alert")).to_contain_text(expected[failure])
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    assert page.evaluate("window.voiceMocks.requests.length") == 0
    assert page.evaluate("window.voiceMocks.contexts.length") == 0


@pytest.mark.parametrize(
    ("code", "reason", "socket_error"),
    [
        (1008, "Rate limit exceeded", False),
        (1011, "Realtime service unavailable", False),
        (1006, "", False),
        (1011, "Realtime service unavailable", True),
    ],
    ids=["rate-limit", "upstream-failure", "abnormal-close", "socket-error"],
)
def test_realtime_voice_backend_failure_releases_audio_and_can_restart(
    page: Page, live_server_url: str, realtime_voice, code: int, reason: str, socket_error: bool
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("window.voiceMocks.sockets[0].message({type: 'response.audio.delta', delta: 'AAA='})")
    assert page.evaluate("window.voiceMocks.contexts.length") == 2
    if socket_error:
        page.evaluate("window.voiceMocks.sockets[0].fail()")
        expect(page.get_by_role("alert")).to_contain_text("Unable to connect")
        expect_voice_audio_released(page)
    page.evaluate("args => window.voiceMocks.sockets[0].finishClose(...args)", [code, reason])
    expect(page.get_by_role("alert")).to_contain_text(reason or "Unable to connect")
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    expect_voice_audio_released(page)

    start_voice_connection(page, 1)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    page.get_by_role("button", name="Stop Voice Chat", exact=True).click()
    expect_voice_audio_released(page)


def test_realtime_voice_normal_disconnect_preserves_reason_and_releases_audio(
    page: Page, live_server_url: str, realtime_voice
):
    page.goto(f"{live_server_url}#/voice")
    start_voice_connection(page)
    expect(page.get_by_text("Listening", exact=True)).to_be_visible()
    page.evaluate("window.voiceMocks.sockets[0].message({type: 'response.audio.delta', delta: 'AAA='})")
    page.evaluate("window.voiceMocks.sockets[0].finishClose(1000, 'Session ended')")
    expect(page.get_by_text("Session ended", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Start Voice Chat", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    expect_voice_audio_released(page)


def test_web_citation_opens_mocked_source(page: Page, live_server_url: str, api_config):
    source_url = "https://example.test/profile"
    response = chat_response(
        f"Read my published profile [{source_url}].",
        context={
            "data_points": {
                "text": [],
                "images": [],
                "citations": [source_url],
                "external_results_metadata": [{"url": source_url, "title": "Profile reference"}],
            }
        },
    )
    page.context.route(
        source_url, lambda route: route.fulfill(content_type="text/html", body="<h1>Mock profile source</h1>")
    )
    open_saved_answer(page, live_server_url, api_config, response)
    citation = page.get_by_role("link", name="1. Profile reference", exact=True)
    expect(citation).to_have_attribute("href", source_url)
    expect(citation).to_have_attribute("target", "_blank")
    expect(citation).to_have_attribute("rel", "noopener noreferrer")
    with page.expect_popup() as opened:
        citation.click()
    source_page = opened.value
    expect(source_page.get_by_role("heading", name="Mock profile source")).to_be_visible()
    assert source_page.evaluate("window.opener === null")
    source_page.close()


def assert_layout(page: Page) -> None:
    problems = page.evaluate("""() => {
            const problems = [];
            const width = document.documentElement.clientWidth;
            if (document.documentElement.scrollWidth > width + 1 || document.body.scrollWidth > width + 1) {
                problems.push({type: 'horizontal-overflow', width, document: document.documentElement.scrollWidth, body: document.body.scrollWidth});
            }
            const visible = element => {
                const rect = element.getBoundingClientRect();
                return element.checkVisibility({visibilityProperty: true, opacityProperty: true}) &&
                    rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight;
            };
            const describe = element => element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.textContent.trim().slice(0, 80);
            const controls = [...document.querySelectorAll('button, textarea, a[href], [role="button"]')].filter(visible);
            const texts = [...document.querySelectorAll('h1, h2, h3, p, [class*="exampleText"]')].filter(visible);
            const overlaps = (first, second) => {
                const firstRect = first.getBoundingClientRect();
                const secondRect = second.getBoundingClientRect();
                return Math.min(firstRect.right, secondRect.right) - Math.max(firstRect.left, secondRect.left) > 2 &&
                    Math.min(firstRect.bottom, secondRect.bottom) - Math.max(firstRect.top, secondRect.top) > 2;
            };
            controls.forEach((control, index) => {
                const rect = control.getBoundingClientRect();
                if (rect.left < -1 || rect.right > width + 1) problems.push({type: 'control-outside-viewport', control: describe(control)});
                if (control.tagName === 'BUTTON' && control.scrollWidth > control.clientWidth + 1) {
                    problems.push({type: 'button-content-overflow', control: describe(control)});
                }
                [...controls.slice(index + 1), ...texts].forEach(other => {
                    if (!control.contains(other) && !other.contains(control) && overlaps(control, other)) {
                        problems.push({type: 'overlap', control: describe(control), other: describe(other)});
                    }
                });
            });
            return problems;
        }""")
    assert not problems, f"Layout defects: {json.dumps(problems, indent=2)}"


@pytest.mark.parametrize(
    ("width", "height", "size_name"),
    [(1280, 900, "desktop"), (390, 844, "mobile")],
    ids=["desktop", "mobile"],
)
def test_responsive_home_and_conversation(
    page: Page, live_server_url: str, api_config, width: int, height: int, size_name: str
):
    api_config.update(showChatHistoryBrowser=True, showRealtimeVoice=True)
    page.set_viewport_size({"width": width, "height": height})
    answer = "My work includes evaluation, observability, reliable application delivery, and accessible interfaces."
    page.goto(live_server_url)
    expect(page.get_by_text("What projects have you worked on?", exact=True)).to_be_visible()
    page.wait_for_function("[...document.images].every(image => image.complete && image.naturalWidth > 0)")
    capture_screenshot(page, f"{size_name}-home")
    assert_layout(page)
    question = (
        "Please describe your recent projects, the engineering trade-offs you made, and how you measured their impact."
    )
    seed_history(page, 1, chat_response(answer), question)
    page.get_by_role("button", name="Open chat history").click()
    page.get_by_role("button", name="Saved conversation 00", exact=True).click()
    expect(page.get_by_text(question, exact=True)).to_be_visible()
    expect(page.get_by_text(answer, exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Try voice chat", exact=True)).to_be_enabled()
    page.wait_for_function("""() => [...document.querySelectorAll('[class*="answerWrapper"]')].every(element => {
            const transform = getComputedStyle(element.parentElement).transform;
            return transform === 'none' || Math.abs(new DOMMatrixReadOnly(transform).m42) < 0.1;
        })""")
    capture_screenshot(page, f"{size_name}-chat")
    assert_layout(page)
