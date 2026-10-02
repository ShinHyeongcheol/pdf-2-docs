import test from 'node:test';
import assert from 'node:assert/strict';
import {runNotionBridge} from './notion_bridge_host.mjs';

const request = (tool, args) => JSON.stringify({type: 'mcp_request', id: 'request-1', tool, arguments: args}) + '\n';
const config = () => ({hubId: 'hub', pageIds: ['lesson'], command: 'reviewed command', workdir: '/private',
  execCommand: async () => ({session_id: 7, output: request('notion_fetch', {id: 'lesson'})}),
  notionFetch: async () => ({content: []}), notionUpdatePage: async () => ({content: []})});

test('complete linked RPC returns result and persists raw responses', async () => {
  const options = config(), saved = [], mutations = [];
  let step = 0;
  options.saveResponse = async (...args) => saved.push(args);
  options.notionUpdatePage = async args => { mutations.push(args); return {content: []}; };
  options.writeStdin = async args => {
    assert.equal(args.session_id, 7);
    assert.equal(JSON.parse(args.chars).id, 'request-1');
    if (++step === 1) return {session_id: 7, output: request('notion_update_page', {
      page_id: 'hub', command: 'insert_content', position: {type: 'end'}, content: 'reviewed literal', allow_async: false})};
    return {exit_code: 0, output: JSON.stringify({type: 'result', result: {status: 'published_readback_verified', actual_notion_appends: 1}}) + '\n'};
  };
  const actual = await runNotionBridge(options);
  assert.equal(actual.host_calls, 2); assert.equal(actual.host_writes, 1);
  assert.equal(saved.length, 2); assert.equal(mutations.length, 1);
});

test('different destination is blocked without a connector call and session is interrupted', async () => {
  const options = config(); let reads = 0, interrupted = 0;
  options.execCommand = async () => ({session_id: 7, output: request('notion_fetch', {id: 'other'})});
  options.notionFetch = async () => { reads++; };
  options.writeStdin = async args => { assert.equal(args.chars, '\u0003'); interrupted++; };
  await assert.rejects(runNotionBridge(options), /outside reviewed destination/);
  assert.equal(reads, 0); assert.equal(interrupted, 1);
});

test('lost mutation response interrupts process and does not repeat dispatch', async () => {
  const options = config(); let writes = 0, interrupted = 0;
  options.execCommand = async () => ({session_id: 7, output: request('notion_update_page', {
    page_id: 'hub', command: 'insert_content', position: {type: 'end'}, content: 'reviewed literal', allow_async: false})});
  options.notionUpdatePage = async () => { writes++; throw new Error('unknown connector result'); };
  options.writeStdin = async args => { assert.equal(args.chars, '\u0003'); interrupted++; };
  await assert.rejects(runNotionBridge(options), /unknown connector result/);
  assert.equal(writes, 1); assert.equal(interrupted, 1);
});

test('mixed append request and completion are rejected before mutation', async () => {
  const options = config(); let writes = 0;
  options.execCommand = async () => ({session_id: 7, output: request('notion_update_page', {
    page_id: 'hub', command: 'insert_content', position: {type: 'end'}, content: 'reviewed literal', allow_async: false}) +
    JSON.stringify({type: 'result', result: {status: 'unchanged', actual_notion_appends: 0}}) + '\n'});
  options.notionUpdatePage = async () => { writes++; };
  options.writeStdin = async args => { assert.equal(args.chars, '\u0003'); };
  await assert.rejects(runNotionBridge(options), /Mixed or duplicate completion/);
  assert.equal(writes, 0);
});

test('duplicate completion is rejected', async () => {
  const options = config();
  const done = JSON.stringify({type: 'result', result: {status: 'unchanged'}}) + '\n';
  options.execCommand = async () => ({session_id: 7, output: done + done});
  options.writeStdin = async args => { assert.equal(args.chars, '\u0003'); };
  await assert.rejects(runNotionBridge(options), /Mixed or duplicate completion/);
});

test('completion count disagrees with dispatched write and is rejected', async () => {
  const options = config();
  options.execCommand = async () => ({exit_code: 0, output: JSON.stringify({type: 'result',
    result: {status: 'published_readback_verified', actual_notion_appends: 1}}) + '\n'});
  await assert.rejects(runNotionBridge(options), /disagrees with dispatched writes/);
});
