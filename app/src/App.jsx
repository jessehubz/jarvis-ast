import { useState, useEffect, useRef, useCallback } from 'react'

const IS_ISLAND = new URLSearchParams(window.location.search).get('view') === 'island'
const WS_URL    = 'ws://localhost:8765'

// ── WebSocket hook ─────────────────────────────────────────────────────────

function useVoice(onEvent) {
  const wsRef = useRef(null)
  const timerRef = useRef(null)

  const connect = useCallback(() => {
    // Guard: don't open a second connection if one is already live.
    // React StrictMode unmount/remount causes onclose to fire and schedule a
    // reconnect timer. If the remount's new WS connects before the timer fires,
    // the timer would create a second live connection and every server event
    // would be processed twice.
    const state = wsRef.current?.readyState
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      onEvent({ type: '_connected' })
      clearTimeout(timerRef.current)
    }
    ws.onmessage = (e) => {
      try { onEvent(JSON.parse(e.data)) } catch {}
    }
    ws.onclose = () => {
      onEvent({ type: '_disconnected' })
      timerRef.current = setTimeout(connect, 2000)
    }
    ws.onerror = () => ws.close()
  }, [onEvent])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  return send
}

export default function App() {
  return IS_ISLAND ? <Island /> : <ChatWindow />
}

// ─────────────────────────────────────────────────────────────────────────────
//  FLOATING ISLAND
// ─────────────────────────────────────────────────────────────────────────────

function Island() {
  const [status,   setStatus]   = useState('Connecting…')
  const [lastText, setLastText] = useState('')
  const [mode,     setMode]     = useState('idle') // idle | listen | record | think | offline
  const [expanded, setExpanded] = useState(false)

  const handleEvent = useCallback((ev) => {
    switch (ev.type) {
      case '_connected':    setMode('idle');   setStatus('Connected'); break
      case '_disconnected': setMode('offline'); setStatus('Offline — start voice_server.py'); break
      case 'ready':         setMode('listen'); setStatus('Listening…'); break
      case 'status':
        setStatus(ev.text)
        if      (ev.text.includes('Record'))                           setMode('record')
        else if (ev.text.includes('Think') || ev.text.includes('Transcrib')) setMode('think')
        else if (ev.text.includes('Listen'))                           setMode('listen')
        else                                                           setMode('idle')
        break
      case 'woke':  setMode('record'); setStatus('Recording…'); break
      case 'heard': setLastText(ev.text); break
    }
  }, [])

  useVoice(handleEvent)

  const onEnter = () => { setExpanded(true);  window.electron?.resizeIsland(true) }
  const onLeave = () => { setExpanded(false); window.electron?.resizeIsland(false) }

  return (
    <div className="island-wrap" onMouseEnter={onEnter} onMouseLeave={onLeave}>
      <div className="island-pill">
        <span className={`island-dot mode-${mode}`} />
        <span className="island-label">{status}</span>
      </div>
      {expanded && lastText && (
        <p className="island-heard">"{lastText.length > 60 ? lastText.slice(0,60)+'…' : lastText}"</p>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
//  CHAT WINDOW
// ─────────────────────────────────────────────────────────────────────────────

const INIT = [{ id: 0, role: 'j', text: 'Hello. Say "hey jarvis" or type a message.', done: true }]

function ChatWindow() {
  const [msgs,     setMsgs]     = useState(INIT)
  const [status,   setStatus]   = useState('Connecting to voice server…')
  const [mode,     setMode]     = useState('offline')
  const [input,    setInput]    = useState('')
  const streamRef  = useRef('')
  const bottomRef  = useRef(null)
  const sendRef    = useRef(null)

  const scroll = () => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  useEffect(scroll, [msgs])

  const handleEvent = useCallback((ev) => {
    switch (ev.type) {
      case '_connected':
        setStatus('Connected — loading models…')
        setMode('idle')
        break
      case '_disconnected':
        setStatus('⚠ Voice server offline  —  run: python voice_server.py')
        setMode('offline')
        break
      case 'ready':
        setStatus('Listening…')
        setMode('listen')
        break
      case 'status':
        setStatus(ev.text)
        if      (ev.text.includes('Record'))                                setMode('record')
        else if (ev.text.includes('Think') || ev.text.includes('Transcrib')) setMode('think')
        else if (ev.text.includes('Listen'))                                 setMode('listen')
        else                                                                 setMode('idle')
        break
      case 'woke':
        setMode('record')
        setStatus('Recording…')
        break
      case 'heard':
        streamRef.current = ''
        setMsgs(m => [
          ...m,
          { id: Date.now(),     role: 'u', text: ev.text, done: true },
          { id: Date.now() + 1, role: 'j', text: '',      done: false },
        ])
        break
      case 'token':
        streamRef.current += ev.text
        setMsgs(m => {
          const u = [...m]
          const last = u[u.length - 1]
          if (last && !last.done) u[u.length - 1] = { ...last, text: streamRef.current }
          return u
        })
        break
      case 'done':
        setMsgs(m => {
          const u = [...m]
          const last = u[u.length - 1]
          if (last && !last.done) u[u.length - 1] = { ...last, done: true }
          return u
        })
        break
      case 'warn':
        setMsgs(m => [...m, { id: Date.now(), role: 'j', text: `⚠ ${ev.text}`, done: true, warn: true }])
        break
      case 'error':
        setStatus('⚠ ' + ev.text)
        setMsgs(m => {
          const u = [...m]
          const last = u[u.length - 1]
          if (last && !last.done) {
            u[u.length - 1] = { ...last, text: ev.text, done: true, err: true }
          } else {
            u.push({ id: Date.now(), role: 'j', text: ev.text, done: true, err: true })
          }
          return u
        })
        break
    }
  }, [])

  const ws = useVoice(handleEvent)
  sendRef.current = ws

  const send = () => {
    const text = input.trim()
    if (!text) return
    setInput('')
    sendRef.current({ type: 'text', text })
  }

  const onKey = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }

  return (
    <div className="app">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="drag-region" />
        <div className="logo">JARVIS</div>
        <nav className="nav">
          <button className="nav-btn active">Chat</button>
          <button className="nav-btn">History</button>
          <button className="nav-btn">Settings</button>
        </nav>
        <div className="sidebar-status">
          <span className={`dot mode-${mode}`} />
          <span className="status-label">{status}</span>
        </div>
      </aside>

      {/* Main */}
      <div className="main">
        {/* Titlebar */}
        <div className="titlebar">
          <button className="tl red"    onClick={() => window.electron?.close()} />
          <button className="tl yellow" onClick={() => window.electron?.minimize()} />
          <button className="tl green" />
          <span className="win-title">Chat</span>
        </div>

        {/* Messages */}
        <div className="messages">
          {msgs.map(msg => (
            <div key={msg.id} className={`row row-${msg.role}`}>
              {msg.role === 'j' && <div className="avatar">J</div>}
              <div className={`bubble bubble-${msg.role}${msg.err ? ' bubble-err' : ''}${msg.warn ? ' bubble-warn' : ''}`}>
                {msg.text || (!msg.done ? <span className="cursor" /> : null)}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="input-row">
          <input
            className="input"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder='Type a message or say "hey jarvis"…'
          />
          <button className="send" onClick={send}>↑</button>
        </div>
      </div>
    </div>
  )
}
