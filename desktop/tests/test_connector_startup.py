"""Regress the reported commands startup errors using actual bundled JS calls.

The sandbox models Chrome exposing ``commands`` only when it is declared in
the manifest. It does not execute the full worker or launch a real browser.
"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONNECTOR_MANIFEST = ROOT / "desktop" / "browser-extension" / "manifest.json"


class ConnectorCommandManifestTests(unittest.TestCase):
    def test_original_background_commands_exist_without_claiming_shortcuts(self):
        original = json.loads((ROOT / "manifest.json").read_text("utf-8"))["commands"]
        connector = json.loads(CONNECTOR_MANIFEST.read_text("utf-8"))["commands"]
        self.assertEqual(set(connector), set(original))
        self.assertEqual(len(connector), 5)
        for name, command in connector.items():
            with self.subTest(command=name):
                self.assertEqual(command["description"], original[name]["description"])
                self.assertNotIn("suggested_key", command)


NODE_SETUP = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const manifest = JSON.parse(fs.readFileSync('desktop/browser-extension/manifest.json', 'utf8'));
const background = fs.readFileSync('assets/background.js', 'utf8');

// Extract the shipped calls, including their callbacks, rather than replace
// the API access that failed with a test-specific implementation.
function originalCall(marker, from = 0) {
  const start = background.indexOf(marker, from);
  assert(start >= 0, `Original background call missing: ${marker}`);
  const opening = start + marker.length - 1;
  assert.equal(background[opening], '(');
  let depth = 0, quote = null, escaped = false;
  for (let i = opening; i < background.length; i++) {
    const char = background[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'" || char === '`') { quote = char; continue; }
    if (char === '(') depth++;
    else if (char === ')' && --depth === 0) {
      return {code: background.slice(start, i + 1), end: i + 1};
    }
  }
  throw new Error(`Unclosed original background call: ${marker}`);
}
const registration = originalCall('Ka.commands.onCommand.addListener(');
const fulltextQuery = originalCall('Ka.commands.getAll(');
const allCommandsQuery = originalCall('Ka.commands.getAll(', fulltextQuery.end);
assert.equal(background.indexOf('Ka.commands.getAll(', allCommandsQuery.end), -1);

function sandbox(specification) {
  const listeners = [], handled = [], results = [];
  const declared = Object.entries(specification.commands || {});
  // This is an explicit API-availability model, not an inspection of Chrome.
  const commands = declared.length ? {
    onCommand: {addListener: callback => listeners.push(callback)},
    getAll: callback => callback(declared.map(([name, command]) => ({
      name, description: command.description, shortcut: ''
    })))
  } : undefined;
  const context = vm.createContext({
    Ka: {platform: 'chrome', commands},
    fu: (_receiver, _args, generator) => generator().next().value,
    t: command => handled.push(command),
    n: 'fulltext-translate',
    e: result => results.push(result)
  });
  return {listeners, handled, results,
    run: fragment => vm.runInContext(fragment.code, context, {timeout: 1000})};
}
function previousManifest() {
  const {commands, ...previous} = manifest;
  return previous;
}
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the original background sandbox")
class ConnectorCommandStartupTests(unittest.TestCase):
    def run_node(self, script):
        result = subprocess.run([shutil.which("node"), "-e", NODE_SETUP + "\n" + script],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_previous_manifest_reproduces_oncommand_type_error(self):
        self.run_node(r'''
const old = sandbox(previousManifest());
assert.throws(() => old.run(registration), {
  name: 'TypeError', message: /Cannot read properties of undefined \(reading 'onCommand'\)/
});
assert.equal(old.listeners.length, 0);
''')

    def test_previous_manifest_reproduces_both_getall_type_errors(self):
        self.run_node(r'''
for (const query of [fulltextQuery, allCommandsQuery]) {
  const old = sandbox(previousManifest());
  assert.throws(() => old.run(query), {
    name: 'TypeError', message: /Cannot read properties of undefined \(reading 'getAll'\)/
  });
  assert.equal(old.results.length, 0);
}
''')

    def test_restored_manifest_registers_the_original_listener(self):
        self.run_node(r'''
const current = sandbox(manifest);
current.run(registration);
assert.equal(current.listeners.length, 1);
current.listeners[0]('caption-toggle');
assert.deepEqual(current.handled, ['caption-toggle']);
''')

    def test_restored_manifest_supports_both_original_command_queries(self):
        self.run_node(r'''
const current = sandbox(manifest);
current.run(fulltextQuery);
assert.equal(current.results.length, 1);
assert.equal(current.results[0].name, 'fulltext-translate');
assert.equal(current.results[0].description, manifest.commands['fulltext-translate'].description);
current.run(allCommandsQuery);
assert.equal(current.results.length, 2);
assert.equal(current.results[1].length, 5);
assert.deepEqual(current.results[1].map(command => command.name).sort(), Object.keys(manifest.commands).sort());
assert(current.results[1].every(command => command.shortcut === ''));
''')


if __name__ == "__main__":
    unittest.main()
