from pathlib import Path
import json
import shutil
import subprocess
import textwrap


FRONTEND = (
    Path(__file__).resolve().parents[2] / "frontend" / "index.html"
).read_text(encoding="utf-8")


def test_automation_monitor_filters_by_display_status_mapping() -> None:
    assert "automationMonitorStatusMatches: function(job, filter)" in FRONTEND
    assert "['completed','done','review_pending'].indexOf(status)!==-1" in FRONTEND
    assert "['failed','stalled','attention'].indexOf(health)!==-1" in FRONTEND
    assert "self.automationMonitorStatusMatches(j,m.filter.status)" in FRONTEND


def test_start_and_stop_automation_keep_logical_and_scheduled_switches_aligned() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the automation monitor behavior test"
    frontend_path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        const html = fs.readFileSync({json.dumps(str(frontend_path))}, 'utf8');
        const scripts = Array.from(
          html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/gi),
          function(match) {{ return match[1]; }}
        );
        const appSource = scripts.find(function(source) {{
          return source.indexOf('Vue.createApp({{') !== -1;
        }});
        let appOptions = null;
        const sandbox = {{
          window: {{KB_RUNTIME: {{apiBase: '', baseUrl: ''}}}},
          localStorage: {{getItem: function() {{ return ''; }}}},
          Vue: {{createApp: function(options) {{
            appOptions = options;
            return {{mount: function() {{ return null; }}}};
          }}}},
          console: console,
          URL: URL,
          URLSearchParams: URLSearchParams,
          setTimeout: setTimeout,
          clearTimeout: clearTimeout
        }};
        vm.createContext(sandbox);
        vm.runInContext(appSource, sandbox);
        const context = {{
          answerHubMonitor: {{
            control: {{enabled: false, schedule_enabled: false}}
          }},
          saveAutomationMonitorControl: null
        }};
        Object.keys(appOptions.methods).forEach(function(name) {{
          if (typeof appOptions.methods[name] === 'function') {{
            context[name] = appOptions.methods[name].bind(context);
          }}
        }});
        context.saveAutomationMonitorControl = function() {{
          this.saved = {{
            enabled: this.answerHubMonitor.control.enabled,
            schedule_enabled: this.answerHubMonitor.control.schedule_enabled
          }};
        }};
        context.startAutomationProcess();
        assert.deepStrictEqual(context.saved, {{
          enabled: true,
          schedule_enabled: true
        }});
        context.stopAutomationProcess();
        assert.deepStrictEqual(context.saved, {{
          enabled: false,
          schedule_enabled: false
        }});
        """
    )
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
