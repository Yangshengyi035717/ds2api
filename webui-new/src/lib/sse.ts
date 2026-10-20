export interface SseDataEvent {
  /** Event name from the `event:` line, or "message" when omitted. */
  event: string
  /** Concatenated `data:` lines without the SSE field prefix. */
  data: string
  /** Complete raw event, useful for debugging views. */
  raw: string
}

/**
 * Consume an SSE response and emit complete events.
 *
 * Both CRLF and LF line endings are accepted. An event may contain multiple
 * `data:` lines, which are joined with newlines as required by the SSE format.
 */
export async function consumeSse(
  response: Response,
  onEvent: (event: SseDataEvent) => void,
): Promise<void> {
  if (!response.body) {
    throw new Error('响应不包含可读取的流')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const emit = (raw: string) => {
    let event = 'message'
    const dataLines: string[] = []

    for (const line of raw.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim() || 'message'
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart())
      }
    }

    if (dataLines.length > 0) {
      onEvent({ event, data: dataLines.join('\n'), raw })
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')

    let separator = buffer.indexOf('\n\n')
    while (separator !== -1) {
      emit(buffer.slice(0, separator))
      buffer = buffer.slice(separator + 2)
      separator = buffer.indexOf('\n\n')
    }
  }

  buffer += decoder.decode()
  if (buffer.trim()) emit(buffer)
}
