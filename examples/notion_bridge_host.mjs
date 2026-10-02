/** Host-supplied connected MCP tools; no credentials or network client here. */
export async function runNotionBridge({execCommand, writeStdin, notionFetch,
  notionUpdatePage, command, workdir, hubId, pageIds, saveResponse = async () => {}}) {
  const allowed = new Set([hubId, ...pageIds]);
  const keysEqual = (value, keys) => Object.keys(value).sort().join() === keys.sort().join();
  let session, buffer = '', result, sequence = 0, writes = 0;
  try {
    let chunk = await execCommand({cmd: command, workdir, tty: true, yield_time_ms: 1000,
      max_output_tokens: 8000});
    for (let turns = 0; turns < 200; turns++) {
      session = chunk.session_id ?? session;
      buffer += chunk.output;
      const lines = buffer.split('\n'); buffer = lines.pop();
      let request;
      for (const raw of lines) {
        if (!raw.trim()) continue;
        const message = JSON.parse(raw.trim());
        if (message.type === 'blocked') throw new Error('CLI blocked publication');
        if (message.type === 'result') { result = message.result; continue; }
        if (message.type !== 'mcp_request' || request || result || !session)
          throw new Error('Unexpected host protocol');
        request = message;
      }
      if (request) {
        if (++sequence > pageIds.length + 3) throw new Error('Bounded host calls exceeded');
        const args = request.arguments;
        let packet;
        if (request.tool === 'notion_fetch' && keysEqual(args, ['id']) && allowed.has(args.id)) {
          packet = await notionFetch(args);
        } else if (request.tool === 'notion_update_page' &&
          keysEqual(args, ['page_id', 'command', 'position', 'content', 'allow_async']) &&
          args.page_id === hubId && args.command === 'insert_content' &&
          keysEqual(args.position, ['type']) && args.position.type === 'end' &&
          args.allow_async === false && typeof args.content === 'string' && ++writes === 1) {
          packet = await notionUpdatePage(args);
        } else throw new Error('Action outside reviewed destination');
        await saveResponse(sequence, request.tool, packet);
        chunk = await writeStdin({session_id: session,
          chars: JSON.stringify({id: request.id, packet}) + '\n', yield_time_ms: 1000,
          max_output_tokens: 8000});
        continue;
      }
      if (chunk.exit_code !== undefined) {
        if (chunk.exit_code !== 0 || !result || buffer.trim()) throw new Error('Incomplete CLI result');
        return {result, host_calls: sequence, host_writes: writes};
      }
      chunk = await writeStdin({session_id: session, chars: '', yield_time_ms: 1000,
        max_output_tokens: 8000});
    }
    throw new Error('Host session exceeded bounded polling');
  } catch (error) {
    if (session) {
      try { await writeStdin({session_id: session, chars: '\u0003', yield_time_ms: 1000,
        max_output_tokens: 1000}); } catch { /* session may already be closed */ }
    }
    throw error;
  }
}
