from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap


FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


def test_candidate_media_can_be_preserved_or_removed_individually() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the candidate media behavior test"
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        const html = fs.readFileSync({json.dumps(str(FRONTEND))}, 'utf8');
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
        const context = {{}};
        Object.keys(appOptions.methods).forEach(function(name) {{
          const method = appOptions.methods[name];
          if (typeof method === 'function') context[name] = method.bind(context);
        }});
        const content = {{blocks: [
          {{type:'text', value:'原始正文。'}},
          {{type:'image', external_url:'https://cdn.example.com/a.jpg', alt:'图A', caption:'案例图A'}},
          {{type:'image', external_url:'https://cdn.example.com/b.jpg', alt:'图B', caption:'案例图B'}},
          {{type:'video', external_url:'https://cdn.example.com/c.mp4', alt:'视频C', caption:'案例视频C'}}
        ]}};
        assert.strictEqual(context.contentPreview(content), '原始正文。');
        const media = context.contentMediaBlocks(content);
        assert.deepStrictEqual(
          JSON.parse(JSON.stringify(media.map(function(item) {{ return item.type; }}))),
          ['image', 'image', 'video']
        );
        const textEdited = JSON.parse(JSON.stringify(
          context.candidateReviewContent('修改后的正文。', content)
        ));
        assert.strictEqual(textEdited.blocks.length, 4);
        assert.strictEqual(textEdited.blocks[1].external_url, 'https://cdn.example.com/a.jpg');
        assert.strictEqual(textEdited.blocks[3].external_url, 'https://cdn.example.com/c.mp4');
        const oneImageRemoved = JSON.parse(JSON.stringify(
          context.candidateReviewContent('修改后的正文。', content, [media[0], media[2]])
        ));
        assert.deepStrictEqual(
          oneImageRemoved.blocks.map(function(item) {{ return item.type; }}),
          ['text', 'image', 'video']
        );
        assert.strictEqual(oneImageRemoved.blocks[1].external_url, 'https://cdn.example.com/a.jpg');
        assert.strictEqual(oneImageRemoved.blocks[2].external_url, 'https://cdn.example.com/c.mp4');
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
