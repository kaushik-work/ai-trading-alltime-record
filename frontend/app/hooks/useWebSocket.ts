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
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  function logout() {
    localStorage.removeItem("aq_token");
    localStorage.removeItem(CACHE_KEY);
    window.location.replace("/login");
  }

  async function fetchSnapshot() {
    if (!url) return;
    const token = localStorage.getItem("aq_token") || "";
    const snapshotUrl = url.replace(/^ws/, "http").replace(/\/ws$/, "/api/snapshot");
    try {
      const res = await fetch(snapshotUrl, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        cache: "no-store",
      });
      if (res.status === 401) { logout(); return; }
      if (!res.ok) return;
      const parsed = await res.json();
      setData(parsed);
      localStorage.setItem(CACHE_KEY, JSON.stringify(parsed));
    } catch {}
  }

  const wsFailCount = useRef(0);

  function connect() {
    const token = localStorage.getItem("aq_token") || "";
    ws.current = new WebSocket(`${url}?token=${token}`);

    ws.current.onopen = () => {
      setConnected(true);
      wsFailCount.current = 0;
    };

    ws.current.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setData(parsed);
        localStorage.setItem(CACHE_KEY, e.data);
      } catch {}
    };

    ws.current.onclose = (e) => {
      setConnected(false);
      // Code 1008 = auth rejected by server — don't retry, go to login
      if (e.code === 1008) { logout(); return; }
      wsFailCount.current += 1;
      // After 5 consecutive failures without ever connecting, token is likely invalid
      if (wsFailCount.current >= 5) { fetchSnapshot(); wsFailCount.current = 0; }
      retry.current = setTimeout(connect, 3000);
    };

    ws.current.onerror = () => ws.current?.close();
  }

  useEffect(() => {
    if (!url) return;
    connect();
    fetchSnapshot();
    poll.current = setInterval(() => {
      if (!ws.current || ws.current.readyState !== WebSocket.OPEN) {
        fetchSnapshot();
      }
    }, 15000);
    return () => {
      if (retry.current) clearTimeout(retry.current);
      if (poll.current) clearInterval(poll.current);
      ws.current?.close();
    };
  }, [url]);

  return { data, connected };
}
