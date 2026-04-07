"use client";
import { useEffect, useRef, useState } from "react";

const CACHE_KEY = "aq_snapshot";

export function useWebSocket(url: string) {
  const [data, setData] = useState<any>(() => {
    try { return JSON.parse(localStorage.getItem(CACHE_KEY) || "null"); } catch { return null; }
  });
  const [connected, setConnected] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);

  function connect() {
    const token = localStorage.getItem("aq_token") || "";
    ws.current = new WebSocket(`${url}?token=${token}`);

    ws.current.onopen = () => setConnected(true);

    ws.current.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setData(parsed);
        localStorage.setItem(CACHE_KEY, e.data);
      } catch {}
    };

    ws.current.onclose = () => {
      setConnected(false);
      retry.current = setTimeout(connect, 3000); // auto-reconnect
    };

    ws.current.onerror = () => ws.current?.close();
  }

  useEffect(() => {
    if (!url) return;
    connect();
    return () => {
      if (retry.current) clearTimeout(retry.current);
      ws.current?.close();
    };
  }, [url]);

  return { data, connected };
}
