from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_PATH = (
    PROJECT_ROOT
    / "cz-knowledge-kb"
    / "knowledge-kb-master"
    / "frontend"
    / "index.html"
)


def test_candidate_preview_separates_text_from_case_media_and_preserves_media() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the CZ frontend behavior test"

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');

        const html = fs.readFileSync({json.dumps(str(FRONTEND_PATH))}, 'utf8');
        const vueSource = fs.readFileSync(
          {json.dumps(str(FRONTEND_PATH.parent / "lib" / "vue.global.prod.js"))},
          'utf8'
        );
        const templateMatch = html.match(
          /<body[^>]*>([\\s\\S]*?)<script\\s+src="lib\\/vue\\.global\\.prod\\.js"/i
        );
        assert.ok(templateMatch, 'CZ Vue template was not found');

        const compileErrors = [];
        const vueSandbox = {{console: console}};
        const decodeHtml = function(value) {{
          return String(value || '')
            .replace(/&quot;/g, '"')
            .replace(/&#39;/g, "'")
            .replace(/&lt;/g, '<')
            .replace(/&gt;/g, '>')
            .replace(/&times;/g, '×')
            .replace(/&amp;/g, '&');
        }};
        vueSandbox.document = {{
          createElement: function() {{
            return {{
              children: [],
              textContent: '',
              set innerHTML(value) {{
                const source = String(value || '');
                const attribute = source.match(/^<div foo="([\\s\\S]*)">$/);
                if (attribute) {{
                  const decoded = decodeHtml(attribute[1]);
                  this.children = [{{
                    getAttribute: function() {{ return decoded; }}
                  }}];
                  this.textContent = decoded;
                  return;
                }}
                this.children = [];
                this.textContent = decodeHtml(
                  source.replace(/<[^>]*>/g, '')
                );
              }}
            }};
          }}
        }};
        vueSandbox.window = vueSandbox;
        vueSandbox.self = vueSandbox;
        vueSandbox.globalThis = vueSandbox;
        vm.createContext(vueSandbox);
        vm.runInContext(vueSource, vueSandbox);
        assert.ok(
          vueSandbox.Vue && typeof vueSandbox.Vue.compile === 'function',
          'Vue template compiler was not loaded'
        );
        const render = vueSandbox.Vue.compile(
          templateMatch[1],
          {{
            onError: function(error) {{
              compileErrors.push(String(error && error.message || error));
            }}
          }}
        );
        assert.strictEqual(typeof render, 'function');
        assert.deepStrictEqual(compileErrors, []);

        const scripts = Array.from(
          html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/gi),
          function(match) {{ return match[1]; }}
        );
        const appSource = scripts.find(function(source) {{
          return source.indexOf('Vue.createApp({{') !== -1;
        }});
        assert.ok(appSource, 'CZ Vue app script was not found');

        let appOptions = null;
        const sandbox = {{
          window: {{KB_RUNTIME: {{apiBase: '', baseUrl: ''}}}},
          localStorage: {{getItem: function() {{ return ''; }}}},
          Vue: {{
            createApp: function(options) {{
              appOptions = options;
              return {{mount: function() {{ return null; }}}};
            }}
          }},
          console: console,
          URL: URL,
          URLSearchParams: URLSearchParams,
          setTimeout: setTimeout,
          clearTimeout: clearTimeout
        }};
        sandbox.window.window = sandbox.window;
        vm.createContext(sandbox);
        vm.runInContext(appSource, sandbox);
        assert.ok(appOptions && appOptions.methods, 'CZ Vue methods were not captured');

        const context = {{}};
        Object.keys(appOptions.methods).forEach(function(name) {{
          const method = appOptions.methods[name];
          if (typeof method === 'function') context[name] = method.bind(context);
        }});

        const content = {{
          blocks: [
            {{type: 'text', value: '镜头型号应按来源证据核对。'}},
            {{
              type: 'image',
              external_url: 'https://cdn.example.com/case-a.jpg',
              alt: '镜头案例图1',
              caption: '来源案例图'
            }},
            {{
              type: 'image',
              external_url: 'https://cdn.example.com/case-b.jpg',
              alt: '镜头案例图2',
              caption: '来源案例图'
            }},
            {{
              type: 'video',
              external_url: 'https://cdn.example.com/case.mp4',
              alt: '镜头案例视频1',
              caption: '来源案例视频（仅供人工播放）'
            }}
          ]
        }};

        assert.strictEqual(
          context.contentPreview(content),
          '镜头型号应按来源证据核对。'
        );
        assert.deepStrictEqual(
          JSON.parse(JSON.stringify(context.contentMediaBlocks(content))).map(
            function(block) {{ return block.type; }}
          ),
          ['image', 'image', 'video']
        );

        const edited = JSON.parse(JSON.stringify(
          context.candidateReviewContent(
            '修改后的正文。',
            content
          )
        ));
        assert.strictEqual(edited.blocks[0].value, '修改后的正文。');
        assert.strictEqual(edited.blocks.length, 4);
        assert.strictEqual(
          edited.blocks[1].external_url,
          'https://cdn.example.com/case-a.jpg'
        );
        assert.strictEqual(edited.blocks[1].caption, '来源案例图');
        assert.strictEqual(
          edited.blocks[3].external_url,
          'https://cdn.example.com/case.mp4'
        );
        assert.strictEqual(
          edited.blocks[3].caption,
          '来源案例视频（仅供人工播放）'
        );
        """
    )

    completed = subprocess.run(
        [node, "-e", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
