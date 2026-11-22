// Reads the /chat-stream SSE body. Events other than `final` and `error` go to onEvent;
// the two terminal events are returned and end the read.
export async function readChatStream(response, onEvent) {
  if (!response.body) return { final: null, error: null };

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final = null;
  let error = null;

  const processBlock = (block) => {
    let eventName = "message";
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (!dataLines.length) return;

    let payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }

    if (eventName === "final") {
      final = payload;
    } else if (eventName === "error") {
      error = payload;
    } else {
      onEvent(eventName, payload);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    let splitAt = buffer.indexOf("\n\n");
    while (splitAt !== -1) {
      const block = buffer.slice(0, splitAt).trim();
      buffer = buffer.slice(splitAt + 2);
      if (block) processBlock(block);
      splitAt = buffer.indexOf("\n\n");
    }

    if (final || error) {
      await reader.cancel().catch(() => {});
      break;
    }
  }

  return { final, error };
}
