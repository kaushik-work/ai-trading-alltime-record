"use client";
import { useEffect, useRef, useState } from "react";

export function useWebSocket(url: string) {
  const [data, setData] = useState<any>(null);
  const [connected, setConnected] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);

  function connect() {
    const token = localStorage.getItem("aq_token") || "";
    ws.current = new WebSocket(`${url}?token=${token}`);

    ws.current.onopen = () => setConnected(true);

    ws.current.onmessage = (e) => {
      try { setData(JSON.parse(e.data)); } catch {}
    };

    ws.current.onclose = () => {
      setConnected(false);
      retry.current = setTimeout(connect, 3000); // auto-reconnect
    };

    ws.current.onerror = () => ws.current?.close();
  }

  useEffect(() => {
    connect();
    return () => {
      if (retry.current) clearTimeout(retry.current);
      ws.current?.close();
    };
  }, [url]);

  return { data, connected };
}
