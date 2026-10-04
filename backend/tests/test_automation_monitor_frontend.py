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


def test_monitor_run_rows_use_batch_feedback_and_automatic_handling_columns() -> None:
    assert "运行批次" in FRONTEND
    assert "运行状态" in FRONTEND
    assert "任务反馈" in FRONTEND
    assert "异常状态" in FRONTEND
    assert "automationMonitorBatchName" in FRONTEND
    assert "automationMonitorFailureReason" in FRONTEND
    assert "automationMonitorAutoHandlingLabel" in FRONTEND
    assert "saveAutomationMonitorFeedback" not in FRONTEND


def test_monitor_run_rows_explain_incomplete_history_and_failure_state() -> None:
    assert "历史批次（未记录沉淀范围）" in FRONTEND
    assert "任务正常，无失败反馈。" in FRONTEND
    assert "CZ 候选价值复核同步失败，请查看同步阶段错误。" in FRONTEND
    assert "无需处理" in FRONTEND
    assert "处理中" in FRONTEND
    assert "已恢复" in FRONTEND


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


def test_cursor_plan_save_keeps_cursor_fields_in_request() -> None:
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
        let requestBody = null;
        const context = {{
          answerHubMonitor: {{
            control: {{enabled: true, schedule_enabled: true, schedule_time: '02:00', plan: {{
              cursor_date: '2026-09-17', window_days: 3, schedule_frequency: 'daily',
              schedule_weekday: 0, catchup_enabled: true, max_catchup_days: 7,
              timezone: 'Asia/Shanghai'
            }}}}
          }}
        }};
        Object.keys(appOptions.methods).forEach(function(name) {{
          if (typeof appOptions.methods[name] === 'function') {{
            context[name] = appOptions.methods[name].bind(context);
          }}
        }});
        sandbox.fetch = function(_url, options) {{
          requestBody = JSON.parse(options.body);
          return Promise.resolve({{ok:true, json:function() {{ return Promise.resolve({{control:context.answerHubMonitor.control}}); }}}});
        }};
        context.saveAutomationMonitorControl();
        setTimeout(function() {{
          assert.strictEqual(requestBody.cursor_date, '2026-09-17');
          assert.strictEqual(requestBody.window_days, 3);
          assert.strictEqual(requestBody.schedule_frequency, 'daily');
          process.exit(0);
        }}, 0);
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


def test_cursor_plan_is_exposed_without_requiring_legacy_date_fields() -> None:
    assert "起始游标日期" in FRONTEND
    assert "当前待处理" in FRONTEND
    assert "本批结束" in FRONTEND
    assert "最近成功" in FRONTEND
    assert "失败时日期游标不会移动" in FRONTEND
    assert "cursor_date:plan.cursor_date||''" in FRONTEND
    assert "window_days:Number(plan.window_days||1)" in FRONTEND


# ---------------------------------------------------------------------------
# 「自动化流程监管」按钮状态
#
# 线上现象：右上角写「自动化已开始」，但「开始自动化流程」看起来还能点、
# 「停止自动化流程」看起来是灰的。真实原因是两条：
#   1. 旧的 :disabled 逻辑本身是对的，但禁用态只是把品牌绿按钮降到 50% 透明，
#      看起来仍然像主按钮；而可点的「停止」是白底描边，看起来才像禁用。
#   2. 「开始」会在请求发出前就把本地状态改成「已开始」，请求失败后不回滚，
#      于是状态标签显示了一个并没有生效的状态。
# 下面这些用例把这两点都钉住。
# ---------------------------------------------------------------------------

_APP_LOADER = """
    const assert = require('assert');
    const fs = require('fs');
    const vm = require('vm');
    const html = fs.readFileSync({frontend_path}, 'utf8');
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
    function buildContext(overrides) {{
      const context = {{
        currentUser: {{permissions: ['*'], role: 'super_admin'}},
        answerHubMonitor: {{
          loading: false,
          logsLoading: false,
          actionLoading: false,
          updatedAt: '-',
          page: 1,
          pageSize: 20,
          lastError: '',
          errorPinned: false,
          controlRollback: null,
          service: {{status: 'online', message: ''}},
          summary: {{pending: 0, running: 0, attention: 0, completed: 0, cz_sync_failed: 0}},
          jobs: [],
          filter: {{status: '', keyword: ''}},
          log: {{name: '', content: ''}},
          control: {{enabled: false, schedule_enabled: false, running: false, installed: true, available: true, plan: {{}}}}
        }}
      }};
      Object.keys(appOptions.methods).forEach(function(name) {{
        if (typeof appOptions.methods[name] === 'function') {{
          context[name] = appOptions.methods[name].bind(context);
        }}
      }});
      Object.assign(context.answerHubMonitor, (overrides || {{}}).answerHubMonitor || {{}});
      Object.assign(context.answerHubMonitor.control, ((overrides || {{}}).answerHubMonitor || {{}}).control || {{}});
      if ((overrides || {{}}).currentUser) context.currentUser = overrides.currentUser;
      context.loadAutomationMonitor = function() {{ context.reloaded = true; }};
      return context;
    }}
"""


def _run_node_script(body: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the automation monitor behavior test"
    frontend_path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    script = textwrap.dedent(
        _APP_LOADER.format(frontend_path=json.dumps(str(frontend_path))) + textwrap.dedent(body)
    )
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_start_and_stop_buttons_follow_the_real_automation_state() -> None:
    _run_node_script(
        """
        const stopped = buildContext({});
        assert.strictEqual(stopped.automationMonitorActionDisabled('start'), false, '已停止时「开始」应可点');
        assert.strictEqual(stopped.automationMonitorActionDisabled('stop'), true, '已停止时「停止」应禁用');

        const started = buildContext({answerHubMonitor: {control: {enabled: true}}});
        assert.strictEqual(started.automationMonitorActionDisabled('start'), true, '已开始时「开始」应禁用');
        assert.strictEqual(started.automationMonitorActionDisabled('stop'), false, '已开始时「停止」应可点');

        const running = buildContext({answerHubMonitor: {control: {enabled: true, running: true}}});
        assert.strictEqual(running.automationMonitorActionDisabled('stop'), false);
        assert.strictEqual(running.automationMonitorActionDisabled('run'), true, '运行中不能再次立即执行');
        assert.strictEqual(running.automationMonitorActionDisabled('retry'), true, '运行中不能重试失败任务');
        process.exit(0);
        """
    )


def test_run_and_retry_are_disabled_when_state_or_task_forbids_it() -> None:
    _run_node_script(
        """
        // 已停止：两个会推进队列的按钮都不能点
        const stopped = buildContext({});
        assert.strictEqual(stopped.automationMonitorActionDisabled('run'), true);
        assert.strictEqual(stopped.automationMonitorActionDisabled('retry'), true);

        // 计划任务未安装：即使自动化处于「已开始」，也不能点这两个按钮
        const missingTask = buildContext({
          answerHubMonitor: {control: {enabled: true, installed: false, available: false}}
        });
        assert.strictEqual(missingTask.automationMonitorActionDisabled('start'), true, '已开始时「开始」禁用');
        assert.strictEqual(missingTask.automationMonitorActionDisabled('stop'), false, '已开始时「停止」可点');
        assert.strictEqual(missingTask.automationMonitorActionDisabled('run'), true);
        assert.strictEqual(missingTask.automationMonitorActionDisabled('retry'), true);
        assert.ok(missingTask.automationMonitorActionTitle('start').indexOf('未安装') !== -1, '悬停要说明为什么点不了');

        // 服务不可用：状态未知，所有会改状态的按钮都禁用
        const offline = buildContext({
          answerHubMonitor: {service: {status: 'unavailable', message: '暂时无法连接 Answer Hub 服务，请检查服务和网络。'}}
        });
        ['start', 'stop', 'run', 'retry'].forEach(function(action) {
          assert.strictEqual(offline.automationMonitorActionDisabled(action), true, action);
        });
        assert.ok(offline.automationMonitorAlert().indexOf('Answer Hub') !== -1);

        // 无 account:manage 权限：按钮禁用（后端仍会独立拦截）
        const noPerm = buildContext({currentUser: {permissions: ['knowledge:view'], role: 'visitor'}});
        ['start', 'stop', 'run', 'retry'].forEach(function(action) {
          assert.strictEqual(noPerm.automationMonitorActionDisabled(action), true, action);
        });
        assert.ok(noPerm.automationMonitorActionTitle('start').indexOf('管理账号') !== -1);
        process.exit(0);
        """
    )


def test_failed_start_rolls_back_the_optimistic_state_and_shows_the_reason() -> None:
    _run_node_script(
        """
        const context = buildContext({});
        sandbox.fetch = function() {
          return Promise.reject(new Error('运行 Answer Hub 的电脑上还没有安装这个自动化计划任务，自动化无法开始或停止。'));
        };
        context.startAutomationProcess();
        assert.strictEqual(context.answerHubMonitor.control.enabled, true, '请求发出前是乐观状态');
        setTimeout(function() {
          assert.strictEqual(context.answerHubMonitor.control.enabled, false, '失败后必须回滚成真实状态');
          assert.strictEqual(context.answerHubMonitor.control.schedule_enabled, false);
          assert.strictEqual(context.answerHubMonitor.controlRollback, null);
          assert.ok(context.answerHubMonitor.lastError.indexOf('还没有安装这个自动化计划任务') !== -1, '失败原因必须留在页面上');
          assert.ok(context.automationMonitorAlert().indexOf('还没有安装这个自动化计划任务') !== -1, '告警条必须显示出来');
          assert.strictEqual(context.automationMonitorActionDisabled('start'), false, '回滚后「开始」应恢复可点');
          assert.strictEqual(context.reloaded, true, '失败后应重新拉取真实状态');
          process.exit(0);
        }, 10);
        """
    )


def test_error_message_survives_the_follow_up_status_refresh() -> None:
    """失败提示不能刚出现就被随后的状态刷新清掉。

    showAutomationMonitorError 写完后端提示后会立刻重新拉取真实状态；
    如果拉取成功无条件清空 lastError，用户就只能看到提示闪一下 ——
    后端好不容易说清楚的失败原因等于被前端自己吞了。
    """
    _run_node_script(
        """
        const context = buildContext({});
        // 换成真实的 loadAutomationMonitor（buildContext 里那个是桩），
        // 让状态刷新真的成功一次。
        Object.keys(appOptions.methods).forEach(function(name) {
          if (typeof appOptions.methods[name] === 'function') {
            context[name] = appOptions.methods[name].bind(context);
          }
        });
        context.authHeaders = function() { return {}; };
        let fetched = 0;
        sandbox.fetch = function() {
          fetched += 1;
          return Promise.resolve({
            ok: true,
            json: function() {
              return Promise.resolve({
                service: {status: 'online', message: ''},
                control: {enabled: false, installed: false, available: false, running: false, message: '\\u81ea\\u52a8\\u5316\\u8ba1\\u5212\\u4efb\\u52a1\\u5c1a\\u672a\\u5b89\\u88c5\\u3002'},
                summary: {},
                jobs: []
              });
            }
          });
        };
        const reason = '\\u8fd0\\u884c Answer Hub \\u7684\\u7535\\u8111\\u4e0a\\u8fd8\\u6ca1\\u6709\\u5b89\\u88c5\\u8fd9\\u4e2a\\u81ea\\u52a8\\u5316\\u8ba1\\u5212\\u4efb\\u52a1';
        context.showAutomationMonitorError(reason);
        assert.ok(context.answerHubMonitor.lastError.indexOf(reason) !== -1, '\\u63d0\\u793a\\u8981\\u7acb\\u523b\\u53ef\\u89c1');
        setTimeout(function() {
          assert.ok(fetched >= 1, '\\u5fc5\\u987b\\u771f\\u7684\\u91cd\\u65b0\\u62c9\\u53d6\\u72b6\\u6001');
          assert.ok(context.answerHubMonitor.lastError.indexOf(reason) !== -1, '\\u5237\\u65b0\\u6210\\u529f\\u540e\\u63d0\\u793a\\u4e0d\\u80fd\\u88ab\\u6e05\\u6389');
          assert.ok(context.automationMonitorAlert().indexOf(reason) !== -1, '\\u544a\\u8b66\\u6761\\u4e0a\\u8981\\u80fd\\u770b\\u5230');
          // 用户自己再点一次「重新拉取状态」时才允许清掉。
          context.loadAutomationMonitor();
          setTimeout(function() {
            assert.strictEqual(context.answerHubMonitor.lastError, '', '\\u7528\\u6237\\u4e3b\\u52a8\\u5237\\u65b0\\u540e\\u63d0\\u793a\\u624d\\u6d88\\u5931');
            process.exit(0);
          }, 20);
        }, 20);
        """
    )


def test_stale_error_is_dropped_by_the_users_own_refresh_even_if_the_reload_failed() -> None:
    """失败提示后面那次自动刷新如果自己失败了，用户手动刷新也必须能把旧提示清掉。

    线上现象：用户看到一句很早以前的 409 文案，点了「刷新」还是那一句。
    原因是 errorPinned 只在自动刷新「成功」时被放掉：自动刷新一旦失败或被中断，
    这个标记就永久留在页面上，之后每一次成功的刷新都会走进
    `if(!errorPinned){lastError=''}` 的假分支 —— 用户明明刷到了新状态，
    告警条却还挂着旧文案。标记必须在请求发出前就消费掉，只覆盖它紧接的那一次刷新。
    """
    _run_node_script(
        """
        const context = buildContext({});
        // 换成真实的 loadAutomationMonitor（buildContext 里那个是桩）
        Object.keys(appOptions.methods).forEach(function(name) {
          if (typeof appOptions.methods[name] === 'function') {
            context[name] = appOptions.methods[name].bind(context);
          }
        });
        context.authHeaders = function() { return {}; };
        let reloadShouldFail = true;
        sandbox.fetch = function() {
          if (reloadShouldFail) return Promise.reject(new Error('network down'));
          return Promise.resolve({
            ok: true,
            json: function() {
              return Promise.resolve({
                service: {status: 'online', message: ''},
                control: {enabled: true, installed: true, available: true, running: false},
                summary: {},
                jobs: []
              });
            }
          });
        };
        context.showAutomationMonitorError('STALE-ERROR-MARKER');
        setTimeout(function() {
          assert.ok(
            context.answerHubMonitor.lastError.indexOf('STALE-ERROR-MARKER') !== -1,
            'reload failed: the reason must still be on screen'
          );
          reloadShouldFail = false;
          context.loadAutomationMonitor();
          setTimeout(function() {
            assert.strictEqual(
              context.answerHubMonitor.lastError, '',
              'one successful user refresh must drop the stale banner'
            );
            assert.strictEqual(
              context.automationMonitorAlert(), '',
              'the alert bar must be gone once the state was re-read'
            );
            process.exit(0);
          }, 20);
        }, 20);
        """
    )


def test_disabled_buttons_are_visually_neutralised() -> None:
    """禁用态不能用品牌绿底色，否则「已禁用」看起来比「可点」还亮。"""
    assert ".automation-action-group .btn.start:disabled" in FRONTEND
    assert ".automation-action-group .btn.stop:disabled" in FRONTEND
    rule_start = FRONTEND.index(".automation-action-group .btn.start:disabled")
    rule = FRONTEND[rule_start:FRONTEND.index("}", rule_start)]
    assert "background:#f2f4f7" in rule
    assert "color:#98a2b3" in rule
    assert "opacity:1" in rule
    assert "opacity:.5" not in rule
    assert "0f9f88" not in rule
    # 模板必须改用统一的判定方法，而不是各写一套表达式
    assert ':disabled="automationMonitorActionDisabled(\'start\')"' in FRONTEND
    assert ':disabled="automationMonitorActionDisabled(\'stop\')"' in FRONTEND
    assert ':disabled="automationMonitorActionDisabled(\'run\')"' in FRONTEND
    assert ':disabled="automationMonitorActionDisabled(\'retry\')"' in FRONTEND
    # 后端返回的提示要能在页面上看到，不能只弹一次 alert
    assert 'class="automation-alert"' in FRONTEND
    assert "automationMonitorAlert()" in FRONTEND

