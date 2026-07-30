from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_loading_script(source: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class PackageTests(unittest.TestCase):
    def test_static_assets_are_packaged(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        for relative in ("index.html", "assets/styles.css", "assets/app.js"):
            path = static / relative
            self.assertTrue(path.is_file(), relative)
            self.assertGreater(path.stat().st_size, 100)

    def test_github_proxy_tests_render_per_source_latency(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        styles = (static / "assets" / "styles.css").read_text(encoding="utf-8")
        api = (ROOT / "src" / "nonebot_plugin_mimo_console" / "api.py").read_text(encoding="utf-8")
        self.assertIn("PROXY_TEST_CONCURRENCY = 2", script)
        self.assertIn('class="radio-option proxy-option"', script)
        self.assertIn('class="proxy-latency"', script)
        self.assertIn("测试中 ${completed}/${targets.length}", script)
        self.assertIn("连通性测试完成", script)
        self.assertIn('.proxy-latency[data-status="success"]', styles)
        self.assertIn('.proxy-latency[data-status="timeout"]', styles)
        self.assertIn('"status": "timeout"', api)

    def test_header_version_is_populated_from_runtime_metadata(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="version-current"', index)
        self.assertNotIn('id="sidebar-version"', index)
        self.assertNotIn("Mimo Console · v0.1.5", index)
        self.assertIn('$("#version-current").textContent', script)
        self.assertNotIn('$("#sidebar-version").textContent', script)

    def test_loaded_plugin_detail_keeps_readme_preview(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("<h3>README</h3>", script)
        self.assertIn('loadPluginReadme(item.name || item.module, "loaded")', script)
        self.assertIn('source === "loaded" ? "/plugins" : "/store/plugins"', script)

    def test_unloaded_source_plugins_remain_manageable(self) -> None:
        result = run_loading_script(
            """
            import nonebot

            nonebot.init(driver="~fastapi")
            assert nonebot.load_plugin("nonebot_plugin_mimo_console") is not None
            from nonebot_plugin_mimo_console.api import merge_source_plugin_records

            items = merge_source_plugin_records(
                [],
                {
                    "nonebot_plugin_broken": {
                        "project": "nonebot-plugin-broken",
                        "repository": "https://github.com/example/broken.git",
                    },
                },
                set(),
            )
            assert items == [
                {
                    "name": "nonebot_plugin_broken",
                    "module": "nonebot_plugin_broken",
                    "title": "nonebot-plugin-broken",
                    "description": "该 GitHub 源码插件已安装，但当前 NoneBot 进程未成功加载",
                    "usage": "",
                    "type": "plugin",
                    "homepage": "https://github.com/example/broken.git",
                    "icon": "",
                    "matchers": 0,
                    "path": "",
                    "config_keys": [],
                    "distribution": "nonebot-plugin-broken",
                    "loaded": False,
                    "disabled": False,
                    "source_project": "nonebot-plugin-broken",
                    "source_repository": "https://github.com/example/broken.git",
                },
            ]
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("item.loaded === false", script)
        self.assertIn('isUnloaded ? "未加载"', script)

    def test_package_management_state_controls_source_actions_and_counts(self) -> None:
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "state.packageManagement\n      && item.source_repository",
            script,
        )
        self.assertIn(
            "state.packageManagement = data.package_management !== false;",
            script,
        )
        dashboard_start = script.index("async function loadDashboard(")
        dashboard_end = script.index("async function loadPlugins(", dashboard_start)
        self.assertNotIn(
            '$("#nav-plugin-count").textContent',
            script[dashboard_start:dashboard_end],
        )
        self.assertIn(
            'result.restart_required ? "warning" : "success"',
            script,
        )
        self.assertIn("重启 NoneBot 后生效", script)

    def test_readme_renderer_uses_standard_markdown_and_safe_html(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        api = (ROOT / "src" / "nonebot_plugin_mimo_console" / "api.py").read_text(encoding="utf-8")
        readme = (ROOT / "src" / "nonebot_plugin_mimo_console" / "readme.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('MarkdownIt("commonmark", {"html": True})', readme)
        self.assertIn('["table", "strikethrough"]', readme)
        self.assertIn('"content_html": render_readme_html(response.text)', api)
        self.assertIn("function sanitizeReadmeHtml(markup, baseUrl", script)
        self.assertIn('"table"', script)
        self.assertIn('"thead"', script)
        self.assertIn('"tbody"', script)
        self.assertIn('"th"', script)
        self.assertIn('"td"', script)
        self.assertIn('template.content.querySelectorAll("*")', script)
        self.assertIn("normalizeWebUrl(attributes.href, baseUrl)", script)
        self.assertIn("normalizeWebUrl(attributes.src, baseUrl)", script)
        self.assertIn("data.content_html", script)
        self.assertNotIn("function renderMarkdown(", script)
        self.assertNotIn("function normalizeReadmeMarkup(", script)
        self.assertIn('"base_url": (', api)
        self.assertIn("raw.githubusercontent.com/{owner}/{repo}/{branch}/", api)

    def test_plugin_detail_uses_layered_glass_surfaces(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        for selector in (
            ".has-custom-bg .detail-overlay-bg",
            ".has-custom-bg .detail-dialog",
            ".has-custom-bg .detail-header",
            ".has-custom-bg .detail-section",
            ".has-custom-bg #detail-readme",
        ):
            self.assertIn(selector, styles)
        self.assertIn("backdrop-filter: blur(18px)", styles)

    def test_github_install_action_is_next_to_store_link(self) -> None:
        index = (ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        actions_start = index.index('<div class="tabs-actions">')
        actions_end = index.index("</div>", actions_start)
        actions = index[actions_start:actions_end]
        self.assertIn('id="github-install-button"', actions)
        self.assertIn("NoneBot 商店 ↗", actions)
        self.assertLess(
            actions.index('id="github-install-button"'), actions.index("NoneBot 商店 ↗")
        )

    def test_custom_background_disables_page_fade(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".has-custom-bg .page", styles)
        self.assertIn("animation: none;", styles)
        self.assertIn("scrollbar-gutter: stable;", styles)
        self.assertIn(".has-custom-bg .plugin-card", styles)
        self.assertIn(".has-custom-bg .config-group", styles)
        self.assertIn(".has-custom-bg .page-header", styles)
        self.assertNotIn(".metric-card:hover", styles)

    def test_sidebar_contains_icon_only_restart_action(self) -> None:
        index = (ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        restart_start = index.index('id="restart-button"')
        restart_end = index.index("</button>", restart_start)
        restart = index[restart_start:restart_end]
        self.assertIn('aria-label="重启服务"', restart)
        self.assertNotIn("<span>重启服务</span>", restart)
        self.assertLess(restart_start, index.index('class="profile-row"'))
        self.assertNotIn("welcome-side", index)

    def test_dashboard_summary_metric_cards_are_removed(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        styles = (static / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn('class="metrics-grid"', index)
        self.assertNotIn('class="metric-card card"', index)
        self.assertNotIn('$("#cpu-value")', script)
        self.assertNotIn("function setBar", script)
        self.assertNotIn(".metrics-grid", styles)
        self.assertNotIn(".metric-card", styles)

    def test_sidebar_status_and_profile_share_card_styles(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".status-indicator,\n.profile-row {", styles)
        self.assertIn(".profile-row,\n  .search-box", styles)
        self.assertIn("height: 58px;", styles)
        self.assertIn(".status-indicator > .status-dot", styles)
        self.assertIn("margin-right: 13px;", styles)
        self.assertIn(
            ".sidebar-footer :is(.status-indicator, .profile-row) > .icon-btn",
            styles,
        )

    def test_related_page_components_share_stack_cards(self) -> None:
        index = (ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(
            index.count('class="page-stack-card page-intro-card card"'),
            4,
        )
        self.assertIn('class="page-stack-card dependency-tools-card card"', index)
        self.assertIn('class="page-stack-card config-workspace-card card"', index)
        self.assertIn(".page-intro-card > :is(.tabs-bar, .notice)", styles)
        self.assertIn(".dependency-tools-card > .toolbar", styles)
        self.assertIn(".config-workspace-card .config-group", styles)

    def test_recent_dashboard_logs_have_no_light_grid_border(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        block_start = styles.index(".recent-logs {")
        block_end = styles.index(".recent-line time", block_start)
        self.assertNotIn("border:", styles[block_start:block_end])

    def test_redundant_current_status_pills_are_removed(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="bg-status-pill"', index)
        self.assertNotIn('id="proxy-status"', index)
        self.assertNotIn("updateProxyStatus", script)

    def test_selecting_no_background_clears_immediately(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="bg-reset"', index)
        self.assertIn("selectBackgroundMode(radio.value)", script)
        self.assertIn('api("/background", { method: "DELETE" })', script)
        self.assertNotIn('mode !== "none" || state.background?.source === "none"', script)
        self.assertNotIn("async function resetBg", script)

    def test_background_mutations_are_serialized_on_server_and_client(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        api = (ROOT / "src" / "nonebot_plugin_mimo_console" / "api.py").read_text(encoding="utf-8")
        self.assertIn("background_lock = asyncio.Lock()", api)
        self.assertGreaterEqual(api.count("async with background_lock:"), 3)
        self.assertIn("let bgMutationChain = Promise.resolve()", script)
        self.assertIn("queueBgMutation(() => api", script)

    def test_agent_service_upgrade_restores_previous_runtime_on_start_failure(self) -> None:
        script = (ROOT / "agent" / "scripts" / "manage-service.sh").read_text(encoding="utf-8")
        self.assertIn('unit_previous="${unit_file}.previous"', script)
        self.assertIn('mv "$previous" "$install_root"', script)
        self.assertIn('mv "$unit_previous" "$unit_file"', script)
        self.assertIn("the previous runtime and unit were restored", script)

    def test_agent_service_installer_keeps_systemd_hardening_baseline(self) -> None:
        script = (ROOT / "agent" / "scripts" / "manage-service.sh").read_text(encoding="utf-8")
        example = (ROOT / "agent" / "examples" / "mimo-console-agent.service").read_text(
            encoding="utf-8"
        )
        directives = (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "LockPersonality=true",
        )
        for directive in directives:
            with self.subTest(directive=directive):
                self.assertIn(directive, script)
                self.assertIn(directive, example)

    def test_package_operation_polling_uses_agent_terminal_state(self) -> None:
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("while (true)", script)
        self.assertNotIn("ABSOLUTE_CEILING_MS", script)
        self.assertIn("MAX_CONSECUTIVE_FAILURES = 1800", script)

    def test_appearance_uses_theme_card_and_auto_saves_remote_background(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "assets" / "app.js").read_text(encoding="utf-8")
        styles = (static / "assets" / "styles.css").read_text(encoding="utf-8")
        appearance_start = index.index('<div class="appearance-grid">')
        appearance_end = index.index("</section>", appearance_start)
        appearance = index[appearance_start:appearance_end]

        self.assertNotIn("<h2>预览</h2>", index)
        self.assertNotIn('id="bg-preview"', index)
        self.assertNotIn('id="bg-url-save"', index)
        self.assertIn('id="bg-url-status"', appearance)
        self.assertLess(appearance.index("<h2>主题</h2>"), appearance.index("<h2>背景来源</h2>"))
        self.assertIn('addEventListener("input", (event) => queueBgUrlSave', script)
        self.assertIn("const BG_URL_AUTO_SAVE_DELAY = 900", script)
        self.assertIn("async function saveBgUrl(url, revision)", script)
        self.assertIn(".appearance-grid .theme-settings { grid-template-columns: 1fr; }", styles)

    def test_radio_selection_dots_are_vertically_centered(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        radio_start = styles.index(".radio-option {")
        radio_end = styles.index(".radio-option:hover", radio_start)
        input_start = styles.index('.radio-option input[type="radio"] {')
        input_end = styles.index("}", input_start)

        self.assertIn("align-items: center;", styles[radio_start:radio_end])
        self.assertIn("margin: 0;", styles[input_start:input_end])

    def test_custom_background_uses_layered_glass_controls(self) -> None:
        styles = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "styles.css"
        ).read_text(encoding="utf-8")
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        for token in (
            "--glass-panel",
            "--glass-control",
            "--glass-inset",
            ".net-item",
            ".radio-option",
            ".config-input",
            ".ring::before",
            ".status-indicator",
        ):
            self.assertIn(token, styles)
        self.assertIn('root.style.setProperty("--control-opacity"', script)
        self.assertIn('root.style.setProperty("--inset-opacity"', script)

    def test_core_config_events_are_scoped_to_config_groups(self) -> None:
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('$$(".config-input", configGroups)', script)
        self.assertIn('$$(".secret-toggle", configGroups)', script)
        self.assertNotIn('$$(".config-input").forEach', script)

    def test_disconnected_docker_agent_remains_visible_in_ui(self) -> None:
        api = (ROOT / "src" / "nonebot_plugin_mimo_console" / "api.py").read_text(encoding="utf-8")
        script = (
            ROOT / "src" / "nonebot_plugin_mimo_console" / "static" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('"mode": "docker-agent"', api)
        self.assertIn('"available": False', api)
        self.assertIn("宿主机 Agent 未连接", script)

    def test_official_plugin_metadata_fields_are_declared(self) -> None:
        source = (ROOT / "src" / "nonebot_plugin_mimo_console" / "__init__.py").read_text(
            encoding="utf-8"
        )
        fields = (
            "name=",
            "description=",
            "usage=",
            "type=",
            "homepage=",
            "config=",
            "supported_adapters=None",
        )
        for field in fields:
            self.assertIn(field, source)

    def test_localstore_is_used_for_runtime_data(self) -> None:
        source = (ROOT / "src" / "nonebot_plugin_mimo_console" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('require("nonebot_plugin_localstore")', source)
        self.assertIn('get_plugin_data_file("auth.json")', source)

    def test_plugin_does_not_import_an_adapter(self) -> None:
        package = ROOT / "src" / "nonebot_plugin_mimo_console"
        source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        self.assertNotIn("nonebot.adapters", source)
        self.assertNotIn("nonebot_adapter_", source)

    def test_store_dependencies_are_declared(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"nb-cli>=1.4.2"', pyproject)
        self.assertIn('"httpx>=0.27.0', pyproject)

    def test_loads_without_asgi_driver_for_noneflow(self) -> None:
        result = run_loading_script(
            """
            import nonebot

            nonebot.init(driver="~none")
            plugin = nonebot.load_plugin("nonebot_plugin_mimo_console")
            assert plugin is not None
            assert plugin.metadata is not None
            assert plugin.metadata.type == "application"
            assert plugin.metadata.supported_adapters is None
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mounts_routes_with_fastapi_driver(self) -> None:
        result = run_loading_script(
            """
            import nonebot

            nonebot.init(driver="~fastapi")
            plugin = nonebot.load_plugin("nonebot_plugin_mimo_console")
            assert plugin is not None
            app = nonebot.get_app()
            paths = set(app.openapi()["paths"])
            assert "/mimo-console/api/auth/status" in paths
            assert "/mimo-console/api/plugins/{plugin_name}/readme" in paths
            assert any(
                getattr(route, "path", "") == "/mimo-console/assets"
                for route in app.routes
            )
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
