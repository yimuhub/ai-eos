"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, describeError, isCancelled, streamPost } from "@/lib/api";
import { notify } from "@/components/Toast";
import NotificationBell from "@/components/NotificationBell";
import CitationList from "@/components/CitationList";
import { BusyHint, ChatProcessBar, ChatProcessCard } from "@/components/ChatProcessCard";
import {
  citationsFromEvent, citationsFromMeta, type Citation,
} from "@/lib/citations";
import {
  applyChatProcessEvent,
  isProcessEvent,
  newChatProcess,
  processFromMeta,
  type ChatProcess,
  type ChatProcessEvent,
} from "@/lib/chatProcess";
import {
  clock, meetingMeta, sessionMeta, taskMeta,
  type GroupMsg, type MeetingDetail, type WsAgent, type WsChatGroup, type WsMeeting, type WsMode, type WsMsg,
  type WsProject, type WsSession, type WsView, type WarDetail,
} from "@/lib/workspace";

/**
 * 中栏：一个对话框，四种会话形态。
 *
 * 单聊走员工流式接口，群聊走项目群聊，会议走会议接口，作战走作战室群聊——
 * 对老板来说都是「在同一个输入框里说话」，只是收件人不同。
 */

type Skill = { id: string; name: string; description?: string | null; source_label?: string };

/** 单聊消息：历史 meta 与流式 done 都归一到 citations/process 上，渲染只用这一份。 */
type ChatMsg = WsMsg & { citations?: Citation[]; process?: ChatProcess | null };
type HistoryMsg = WsMsg & { meta?: unknown };

function fromHistory(rows: HistoryMsg[]): ChatMsg[] {
  return rows.map((m) => ({
    ...m,
    citations: citationsFromMeta(m.meta),
    process: processFromMeta(m.meta),
  }));
}

const SUGGESTIONS = [
  "把这周的任务按优先级排一版，告诉我先做什么",
  "让小红把竞品定价页抓下来，做一版对比表",
  "把上周的交付物汇总成一份周报",
];

const MODES: { key: WsMode; label: string; icon: string }[] = [
  { key: "single", label: "单聊", icon: "◔" },
  { key: "group", label: "群聊", icon: "◕" },
  { key: "meeting", label: "会议", icon: "◉" },
  { key: "war", label: "作战", icon: "◈" },
];

const MODE_HINT: Record<WsMode, string> = {
  single: "对一个人说话，直接给结论",
  group: "群聊：成员依次回应，结论可沉淀",
  meeting: "发言、议程与纪要在同一屏",
  war: "作战现场：过程、分工与交付可回溯",
};

export default function WorkspaceChat({
  view,
  agent,
  agents,
  projects,
  meetings,
  chatGroups,
  sessions,
  cid,
  onMode,
  onOpenView,
  onMeetingCreated,
  onPickAgent,
  onOpenApp,
  onOpenTrace,
  onCollapseOut,
  onExpandLeft,
  onEnsureGroup,
  onEditGroup,
  outHidden,
  leftHidden,
  classic = false,
}: {
  view: WsView;
  agent: WsAgent | null;
  agents: WsAgent[];
  projects: WsProject[];
  meetings: WsMeeting[];
  chatGroups: WsChatGroup[];
  sessions: WsSession[];
  cid: string;
  onMode: (mode: WsMode) => void;
  onOpenView: (view: WsView) => void;
  onMeetingCreated: (m: WsMeeting) => void;
  onPickAgent: (a: WsAgent) => void;
  onOpenApp: (href: string) => void;
  onOpenTrace: () => void;
  onCollapseOut: () => void;
  onExpandLeft: () => void;
  /** 内置群聊缺人：按预设补齐员工并返回建好的群 */
  onEnsureGroup: (group: WsChatGroup) => Promise<WsChatGroup | null>;
  onEditGroup: (group: WsChatGroup) => void;
  outHidden: boolean;
  leftHidden: boolean;
  /** 经典模式：左栏由 ClassicLayout 提供，会议/群聊/作战各有整页入口，这里只有单聊 */
  classic?: boolean;
}) {
  const mode = view.mode;
  const project = useMemo(() => projects.find((p) => p.id === view.id) || null, [projects, view.id]);
  const chatGroup = useMemo(() => chatGroups.find((g) => g.id === view.id) || null, [chatGroups, view.id]);
  const meeting = useMemo(() => meetings.find((m) => m.id === view.id) || null, [meetings, view.id]);
  const session = useMemo(() => sessions.find((s) => s.id === view.id) || null, [sessions, view.id]);
  // 群聊有两个来源：项目群（成员来自项目）和自由群（成员存在 chat_groups）
  const groupScope = chatGroup ? "group" : project ? "project" : null;
  const groupId = chatGroup?.id || project?.id || "";
  const [ensuring, setEnsuring] = useState(false);

  /* ---------------- 单聊 ---------------- */
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const skillsRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 在途流的归属：切会话时不 abort（服务端只在生成完成后落库，断流会丢回复），
  // 只按 key 判断"这段流还该不该往界面上写"。
  const currentKeyRef = useRef(`${view.mode}:${view.id}`);
  currentKeyRef.current = `${view.mode}:${view.id}`;
  // 每次切换自增：即使切走又立刻切回，同一会话的旧流也不再往重拉的历史里写
  const viewEpochRef = useRef(0);
  const streamKeysRef = useRef<Map<AbortController, string>>(new Map());
  // 滚动跟随：只在用户本来就贴着底部时自动滚，上翻读历史时不打扰
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const forceScrollRef = useRef(true);
  // 会议/作战的「结果必达」补拉：有界定时器，切换会话或卸载时清理
  const meetFollowRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const warFollowRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stopMeetFollow = useCallback(() => {
    if (meetFollowRef.current) { clearInterval(meetFollowRef.current); meetFollowRef.current = null; }
  }, []);
  const stopWarFollow = useCallback(() => {
    if (warFollowRef.current) { clearInterval(warFollowRef.current); warFollowRef.current = null; }
  }, []);
  const autoScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (!atBottomRef.current && !forceScrollRef.current) return;
    forceScrollRef.current = false;
    el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    if (mode !== "single" || !agent) { setMsgs([]); return; }
    let alive = true;
    setLoading(true);
    api.get<HistoryMsg[]>(`/agents/${agent.id}/chat/history?limit=50`)
      .then((d) => { if (alive) setMsgs(Array.isArray(d) ? fromHistory(d) : []); })
      .catch(() => { if (alive) setMsgs([]); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [mode, agent]);

  // 切会话：不 abort 在途的流（服务端生成完成后才落库，断流会丢整段回复），
  // 只复位本会话的进度；旧流的回调会因为 key 不匹配而跳过渲染。
  useEffect(() => {
    viewEpochRef.current += 1;
    const key = `${view.mode}:${view.id}`;
    const pending = Array.from(streamKeysRef.current.values()).some((k) => k === key);
    setBusy(pending);
    setStatus("");
    // 别的会话的"等待回应"不能显示在新会话上
    setGroupBusy(false);
    setMeetBusy(false);
    setWarBusy(false);
    atBottomRef.current = true;
    forceScrollRef.current = true;
    // 结果补拉只服务当前会话，切走就停
    stopMeetFollow();
    stopWarFollow();
  }, [view.mode, view.id, stopMeetFollow, stopWarFollow]);

  // 只有离开工作台才真正掐断所有在途流
  useEffect(() => () => {
    streamKeysRef.current.forEach((_, ac) => ac.abort());
    streamKeysRef.current.clear();
    stopMeetFollow();
    stopWarFollow();
  }, [stopMeetFollow, stopWarFollow]);

  useEffect(() => { autoScroll(); }, [msgs, busy, status, autoScroll]);

  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false);
      if (skillsRef.current && !skillsRef.current.contains(e.target as Node)) setSkillsOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const openSkills = useCallback(async () => {
    setSkillsOpen((v) => !v);
    if (skills === null && cid) {
      try {
        setSkills(await api.get<Skill[]>(`/skills?company_id=${cid}`));
      } catch {
        setSkills([]);
      }
    }
  }, [skills, cid]);

  const patchLast = useCallback((patch: (m: ChatMsg) => ChatMsg) => {
    setMsgs((prev) => {
      if (!prev.length) return prev;
      const next = prev.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return next;
    });
  }, []);

  /** 用服务端历史校正当前会话（后台流跑完后补回被跳过的增量）。 */
  const refreshHistory = useCallback(async (agentId: string) => {
    try {
      const d = await api.get<HistoryMsg[]>(`/agents/${agentId}/chat/history?limit=50`);
      if (currentKeyRef.current === `single:${agentId}`) setMsgs(Array.isArray(d) ? fromHistory(d) : []);
    } catch {
      /* 拉不到就等下次切回时再取，不打扰当前界面 */
    }
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (mode !== "single" || !agent || busy || (!text && !files.length)) return;
    const target = agent;
    const sendKey = currentKeyRef.current;
    const sendEpoch = viewEpochRef.current;
    const ac = new AbortController();
    abortRef.current = ac;
    streamKeysRef.current.set(ac, sendKey);
    atBottomRef.current = true;
    forceScrollRef.current = true;
    setBusy(true);
    setStatus("");
    setInput("");
    if (text) setMsgs((p) => [...p, { role: "boss", content: text }]);
    setMsgs((p) => (files.length ? [...p, { role: "boss", content: `📎 已上传 ${files.length} 个附件：${files.map((f) => f.name).join("、")}` }] : p));

    let acc = "";
    let started = false;
    // 一旦切离过这个会话，界面上的 msgs 已被重新拉取，累加器不能再往里写；
    // 之后的增量只记在 acc 里，等 done 后用服务端历史整段补齐。
    let detached = false;
    // 失败/中止时服务端没有这轮回复，刷新历史会把本地的错误卡片抹掉
    let failed = false;
    const stale = () => currentKeyRef.current !== sendKey || viewEpochRef.current !== sendEpoch;
    const live = () => !detached && !stale();
    try {
      const failedFiles: File[] = [];
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        try {
          await api.upload(`/kb2/agents/${target.id}/upload`, fd);
        } catch (e) {
          failedFiles.push(f);
          notify("error", `${f.name} 上传失败：${describeError(e)}`);
        }
      }
      // 只清掉上传成功的附件；失败的保留在输入区，用户可直接重试
      // （修复前无条件清空，失败的那几个看起来像"传完了"）。
      setFiles(failedFiles);
      if (!text) {
        if (!stale()) setMsgs((p) => [...p, { role: "agent", content: "附件已经进我的知识库了，接下来要我拿它们做什么？" }]);
        return;
      }

      if (!stale()) setMsgs((p) => [...p, {
        role: "agent", content: "", streaming: true, process: newChatProcess(),
      }]);
      let serverError = "";
      await streamPost(`/agents/${target.id}/chat/stream`, { message: text }, {
        signal: ac.signal,
        onEvent: (ev) => {
          // 关键：只跳过 UI，不中断请求——服务端要读完这轮才能落库
          if (stale()) { detached = true; return; }
          if (detached) return;
          if (ev.type === "delta" || ev.type === "done" || ev.type === "error"
            || isProcessEvent(ev.type)) {
            patchLast((m) => ({
              ...m,
              process: applyChatProcessEvent(
                m.process ?? newChatProcess(), ev as ChatProcessEvent),
            }));
          }
          if (ev.type === "delta") {
            started = true;
            acc += String(ev.text || "");
            setStatus("");
            patchLast((m) => ({ ...m, content: acc, streaming: true }));
          } else if (ev.type === "status") {
            setStatus(String(ev.text || ""));
          } else if (ev.type === "done") {
            acc = String(ev.reply ?? acc);
            patchLast((m) => ({
              ...m,
              content: acc,
              streaming: false,
              file_name: ev.file?.file_name,
              citations: citationsFromEvent(ev),
            }));
          } else if (ev.type === "error") {
            failed = true;
            serverError = String(ev.message || "服务端处理失败");
          }
        },
      });
      if (serverError && live()) {
        // 错误写进同一个气泡：另起一条会让失败的那轮看起来是「空白回复 + 莫名报错」
        patchLast((m) => ({
          ...m,
          content: acc ? `${acc}\n\n❌ ${serverError}` : `❌ ${serverError}`,
          streaming: false,
        }));
      }
    } catch (e) {
      failed = true;
      if (!live()) {
        // 旧会话已切走：错误不打扰当前界面，回复由服务端下次历史补回
      } else if (ac.signal.aborted || isCancelled(e)) {
        // 主动停止：保留已经收到的字，并说清本轮没有落库
        patchLast((m) => ({
          ...m,
          content: m.content ? `${m.content}\n\n（已停止，这轮回复未保存到历史）` : "（已停止）",
          streaming: false,
          process: applyChatProcessEvent(m.process ?? newChatProcess(), { type: "stopped" }),
        }));
      } else {
        const message = describeError(e, "发送失败");
        patchLast((m) => ({
          ...m,
          content: started ? m.content : `❌ ${message}`,
          streaming: false,
          process: applyChatProcessEvent(
            m.process ?? newChatProcess(), { type: "error", message }),
        }));
      }
    } finally {
      streamKeysRef.current.delete(ac);
      if (abortRef.current === ac) abortRef.current = null;
      // 用 key 而不是 epoch 判断：切走又切回同一会话时，仍要把进度收尾
      if (currentKeyRef.current === sendKey) {
        setBusy(false);
        setStatus("");
        // 后台跑完的那一段没有逐字渲染，用服务端历史把完整回复补回来
        if (!failed && (detached || acc === "")) void refreshHistory(target.id);
      }
    }
  }, [mode, agent, busy, input, files, patchLast, refreshHistory]);

  // 只停当前会话的流：别的会话可能在后台补齐回复，误停会把它一起掐掉
  const stop = useCallback(() => {
    const key = currentKeyRef.current;
    streamKeysRef.current.forEach((k, ac) => { if (k === key) ac.abort(); });
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && mode === "single") {
      e.preventDefault();
      send();
    }
  };

  /* ---------------- 群聊 ---------------- */
  const [groupMsgs, setGroupMsgs] = useState<GroupMsg[]>([]);
  const [groupLoading, setGroupLoading] = useState(false);
  const [groupBusy, setGroupBusy] = useState(false);
  const [groupStartedAt, setGroupStartedAt] = useState<number | null>(null);
  const groupEnd = useRef<HTMLDivElement>(null);

  const loadGroup = useCallback(async () => {
    if (!groupScope || !groupId || !cid) return;
    const key = `group:${groupId}`;
    try {
      const d = await api.get<{ messages: GroupMsg[] }>(
        `/group-chat/${groupScope}/${groupId}/history?company_id=${cid}`);
      // 慢请求回来时用户可能已经切到别的群：别把旧群的消息写进新群
      if (currentKeyRef.current !== key) return;
      // 历史消息的 process 由后端按 content.process 平铺，归约回前端同口径的 ChatProcess
      setGroupMsgs((d.messages || []).map((m) => ({
        ...m,
        process: m.process ? processFromMeta({ process: m.process }) : undefined,
      })));
    } catch {
      if (currentKeyRef.current === key) setGroupMsgs([]);
    }
  }, [groupScope, groupId, cid]);

  useEffect(() => {
    if (mode !== "group" || !groupScope) { setGroupMsgs([]); return; }
    let alive = true;
    setGroupLoading(true);
    loadGroup().finally(() => { if (alive) setGroupLoading(false); });
    return () => { alive = false; };
  }, [mode, groupScope, groupId, loadGroup]);

  useEffect(() => { autoScroll(); }, [groupMsgs, groupBusy, autoScroll]);

  const sendGroup = useCallback(async () => {
    const text = input.trim();
    if (!groupScope || !groupId || !cid || groupBusy || !text) return;
    const sendKey = currentKeyRef.current;
    const sendEpoch = viewEpochRef.current;
    const same = () => currentKeyRef.current === sendKey && viewEpochRef.current === sendEpoch;
    setGroupBusy(true);
    setGroupStartedAt(Date.now());
    setInput("");
    atBottomRef.current = true;
    forceScrollRef.current = true;
    // 先把自己的话放上去，再等员工依次回应（每个员工有专属的 process 下落盘）
    setGroupMsgs((p) => [...p, { role: "boss", text }]);
    try {
      await streamPost(`/group-chat/${groupScope}/${groupId}/stream?company_id=${cid}`,
        { message: text }, {
          onEvent: (ev) => {
            // 切走别的群：这段流不再写回当前界面，让后台跑完落库即可
            if (!same()) return;
            switch (ev.type) {
              case "group_start":
                // 等第一个 agent_start 出现后再往下接
                break;
              case "agent_start":
                setGroupMsgs((p) => [...p, {
                  role: "agent",
                  agent_name: String(ev.agent_name ?? ""),
                  text: "",
                  streaming: true,
                  process: newChatProcess(),
                }]);
                break;
              case "agent_done":
                setGroupMsgs((p) => {
                  const last = p[p.length - 1];
                  if (!last || last.role !== "agent" || !last.streaming) return p;
                  const next = p.slice();
                  next[next.length - 1] = {
                    ...last,
                    // 正文整体落盘，过程随 process 一起保存到消息里
                    text: String(ev.text || last.text),
                    streaming: false,
                    process: last.process
                      ? applyChatProcessEvent(last.process, ev as ChatProcessEvent)
                      : last.process,
                    search_used: ev.search_used === true ? true : last.search_used,
                  };
                  return next;
                });
                break;
              case "group_done":
                setGroupBusy(false);
                setGroupStartedAt(null);
                break;
              case "error":
                // 流式通道挂断：在途 agent 不能再显示"思考中"，补到系统消息里
                notify("error", describeError(new Error(String(ev.message || "群聊流式失败")), "群聊发送失败"));
                setGroupBusy(false);
                setGroupStartedAt(null);
                setGroupMsgs((p) => {
                  const next = p.slice();
                  const last = next[next.length - 1];
                  if (last && last.role === "agent" && last.streaming) {
                    next[next.length - 1] = {
                      ...last,
                      streaming: false,
                      text: `${last.text}${last.text ? "\n" : ""}（流式失败：${ev.message}）`,
                    };
                  }
                  if (next[next.length - 1]?.role !== "system") {
                    next.push({ role: "system", text: `❌ ${describeError(new Error(String(ev.message || "群聊流式失败")), "群聊发送失败")}` });
                  }
                  return next;
                });
                break;
              default:
                // 过程事件与正文逐字（status/model/model_error/reasoning/tool_start/
                // tool_end/done/delta）全部写到当前发言的 agent 头上
                if (!isProcessEvent(ev.type) && ev.type !== "delta" && ev.type !== "done") break;
                setGroupMsgs((p) => {
                  const last = p[p.length - 1];
                  if (!last || last.role !== "agent" || !last.streaming) return p;
                  const next = p.slice();
                  const m = { ...last };
                  m.process = applyChatProcessEvent(
                    m.process ?? newChatProcess(), ev as ChatProcessEvent);
                  if (ev.type === "delta") {
                    m.text = String(m.text || "") + String(ev.text || "");
                  }
                  next[next.length - 1] = m;
                  return next;
                });
            }
          },
        });
    } catch (e) {
      notify("error", describeError(e, "群聊发送失败"));
      if (same()) {
        setGroupBusy(false);
        setGroupStartedAt(null);
        setGroupMsgs((p) => [...p, { role: "system", text: `❌ ${describeError(e, "群聊发送失败")}` }]);
      }
    } finally {
      if (same()) {
        setGroupBusy(false);
        setGroupStartedAt(null);
      }
      if (currentKeyRef.current === sendKey) loadGroup();
    }
  }, [groupScope, groupId, cid, groupBusy, input, loadGroup]);

  /* ---------------- 会议 ---------------- */
  const [meet, setMeet] = useState<MeetingDetail | null>(null);
  const [meetLoading, setMeetLoading] = useState(false);
  const [meetBusy, setMeetBusy] = useState(false);
  const meetEnd = useRef<HTMLDivElement>(null);

  const loadMeeting = useCallback(async (): Promise<MeetingDetail | null> => {
    if (!meeting) return null;
    const key = `meeting:${meeting.id}`;
    try {
      const d = await api.get<MeetingDetail>(`/meetings/${meeting.id}`);
      if (currentKeyRef.current !== key) return null;
      setMeet(d);
      return d;
    } catch {
      if (currentKeyRef.current === key) setMeet(null);
      return null;
    }
  }, [meeting]);

  // 结束后纪要在后台生成：有界补拉（3s × 10），拿到纪要或确认取消才停
  const startMeetFollow = useCallback(() => {
    stopMeetFollow();
    let tries = 0;
    meetFollowRef.current = setInterval(async () => {
      tries += 1;
      const d = await loadMeeting();
      const settled = !!d && (!!d.minutes || !!d.summary || d.status === "CANCELLED");
      if (settled || tries >= 10) stopMeetFollow();
    }, 3000);
  }, [loadMeeting, stopMeetFollow]);

  useEffect(() => {
    if (mode !== "meeting" || !meeting) { setMeet(null); return; }
    let alive = true;
    setMeetLoading(true);
    loadMeeting().finally(() => { if (alive) setMeetLoading(false); });
    return () => { alive = false; };
  }, [mode, meeting, loadMeeting]);

  // 会议在跑的时候员工会一轮轮发言，前端跟着刷新
  useEffect(() => {
    if (mode !== "meeting" || meet?.status !== "RUNNING") return;
    const t = setInterval(loadMeeting, 4000);
    return () => clearInterval(t);
  }, [mode, meet?.status, loadMeeting]);

  useEffect(() => {
    autoScroll();
  }, [meet?.messages.length, meet?.minutes, meet?.summary, meetBusy, autoScroll]);

  // 就算不是在当前页点的「结束」，状态从 RUNNING 落到 COMPLETED 时也补拉纪要
  const meetWasRunningRef = useRef(false);
  useEffect(() => {
    const running = meet?.status === "RUNNING";
    if (meetWasRunningRef.current && !running && meet?.status === "COMPLETED"
      && !meet.minutes && !meet.summary) {
      startMeetFollow();
    }
    meetWasRunningRef.current = running;
  }, [meet?.status, meet?.minutes, meet?.summary, startMeetFollow]);

  const meetingAction = useCallback(async (action: "start" | "pause" | "resume" | "end") => {
    if (!meeting || meetBusy) return;
    setMeetBusy(true);
    try {
      await api.post(`/meetings/${meeting.id}/${action}`, {});
      if (action === "end") notify("success", "已结束，纪要在后台生成");
      await loadMeeting();
      // 补拉只跟随当前会议：请求期间切走就不必再空转
      if (action === "end" && currentKeyRef.current === `meeting:${meeting.id}`) startMeetFollow();
    } catch (e) {
      notify("error", describeError(e, "会议操作失败"));
    } finally {
      setMeetBusy(false);
    }
  }, [meeting, meetBusy, loadMeeting, startMeetFollow]);

  const sendMeeting = useCallback(async () => {
    const text = input.trim();
    if (!meeting || !text || meetBusy) return;
    setMeetBusy(true);
    setInput("");
    try {
      await api.post(`/meetings/${meeting.id}/say`, { content: text });
      await loadMeeting();
    } catch (e) {
      notify("error", describeError(e, "插话失败"));
    } finally {
      setMeetBusy(false);
    }
  }, [meeting, input, meetBusy, loadMeeting]);

  /* ---------------- 作战 ---------------- */
  const [war, setWar] = useState<WarDetail | null>(null);
  const [warChat, setWarChat] = useState<GroupMsg[]>([]);
  const [warLoading, setWarLoading] = useState(false);
  const [warBusy, setWarBusy] = useState(false);
  const warEnd = useRef<HTMLDivElement>(null);

  const loadWar = useCallback(async () => {
    if (!session || !cid) return;
    const key = `war:${session.id}`;
    try {
      const [d, ch] = await Promise.all([
        api.get<WarDetail>(`/war-room/${session.id}?company_id=${cid}`),
        api.get<{ messages: GroupMsg[] }>(`/war-room/sessions/${session.id}/chat?company_id=${cid}`)
          .catch(() => ({ messages: [] as GroupMsg[] })),
      ]);
      if (currentKeyRef.current !== key) return;
      setWar(d);
      setWarChat(ch.messages || []);
    } catch {
      if (currentKeyRef.current === key) setWar(null);
    }
  }, [session, cid]);

  // 发出指令后成员在后台回应：有界补拉（5s × 4），把完成后的尾响应也收进来
  const startWarFollow = useCallback(() => {
    stopWarFollow();
    let tries = 0;
    warFollowRef.current = setInterval(async () => {
      tries += 1;
      await loadWar();
      if (tries >= 4) stopWarFollow();
    }, 5000);
  }, [loadWar, stopWarFollow]);

  useEffect(() => {
    if (mode !== "war" || !session) { setWar(null); setWarChat([]); return; }
    let alive = true;
    setWarLoading(true);
    loadWar().finally(() => { if (alive) setWarLoading(false); });
    return () => { alive = false; };
  }, [mode, session, loadWar]);

  useEffect(() => {
    if (mode !== "war" || !war) return;
    const running = ["analyzing", "announcing", "executing", "delivering"].includes(war.status);
    if (!running) return;
    const t = setInterval(loadWar, 6000);
    return () => clearInterval(t);
  }, [mode, war, loadWar]);

  useEffect(() => {
    autoScroll();
  }, [warChat.length, warBusy, war?.status, autoScroll]);

  // 运行态结束的那一下，最后一条回应可能还在路上：再补拉几轮
  const warWasRunningRef = useRef(false);
  useEffect(() => {
    const running = !!war && ["analyzing", "announcing", "executing", "delivering"].includes(war.status);
    if (warWasRunningRef.current && !running && war) startWarFollow();
    warWasRunningRef.current = running;
  }, [war?.status, war, startWarFollow]);

  const sendWar = useCallback(async () => {
    const text = input.trim();
    if (!session || !cid || !text || warBusy) return;
    const sendKey = currentKeyRef.current;
    setWarBusy(true);
    setInput("");
    atBottomRef.current = true;
    forceScrollRef.current = true;
    setWarChat((p) => [...p, { role: "boss", text }]);
    try {
      await api.post(`/war-room/sessions/${session.id}/say?company_id=${cid}`, { text });
      // 成员是后台回应的：先刷一次自己的话，再按 5s × 4 有界补拉
      await loadWar();
      if (currentKeyRef.current === sendKey) startWarFollow();
    } catch (e) {
      notify("error", describeError(e, "作战室发送失败"));
    } finally {
      if (currentKeyRef.current === sendKey) setWarBusy(false);
    }
  }, [session, cid, input, warBusy, loadWar, startWarFollow]);

  /* ---------------- 发起会议 / 拉群 ---------------- */
  const startMeeting = useCallback(async () => {
    if (!cid) return;
    // 群聊里开会：优先拉本群成员；单聊则把当前员工拉进来
    const participants = chatGroup?.members.length
      ? chatGroup.members.map((m) => m.id)
      : agents.slice(0, 6).map((a) => a.id);
    if (!participants.length) {
      notify("warning", "还没有员工，先招人再开会");
      return;
    }
    const title = `${chatGroup?.name || project?.name || agent?.name || "临时"} · 会议 ${clock(new Date().toISOString())}`;
    try {
      const d = await api.post<{ id: string; title: string }>("/meetings", {
        company_id: cid,
        project_id: project?.id || null,
        title,
        meeting_type: project ? "PROJECT_MEETING" : "FREE_CHAT",
        participant_ids: participants,
        coordinator_agent_id: chatGroup?.members[0]?.id || agent?.id || agents[0].id,
      });
      onMeetingCreated({ id: d.id, title: d.title || title, status: "SCHEDULED" });
      notify("success", "会议已创建，点「开始会议」让员工开口");
    } catch (e) {
      notify("error", describeError(e, "创建会议失败"));
    }
  }, [cid, agents, project, agent, chatGroup, onMeetingCreated]);

  const ensureMissing = useCallback(async () => {
    if (!chatGroup || ensuring) return;
    setEnsuring(true);
    try {
      await onEnsureGroup(chatGroup);
    } finally {
      setEnsuring(false);
    }
  }, [chatGroup, ensuring, onEnsureGroup]);

  const pullGroup = useCallback(() => {
    if (project) { onOpenView({ mode: "group", id: project.id }); return; }
    if (projects[0]) { onOpenView({ mode: "group", id: projects[0].id }); return; }
    onOpenApp("/projects");
  }, [project, projects, onOpenView, onOpenApp]);

  /* ---------------- 通用 ---------------- */
  const submit = useCallback(() => {
    if (mode === "single") send();
    else if (mode === "group") sendGroup();
    else if (mode === "meeting") sendMeeting();
    else sendWar();
  }, [mode, send, sendGroup, sendMeeting, sendWar]);

  const onSubmitKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const headline = (() => {
    if (mode === "single") return { title: agent?.name || "员工", tag: "单聊", sub: agent?.role || "数字员工", av: (agent?.name || "AI").slice(0, 1) };
    if (mode === "group") {
      if (chatGroup) {
        const missing = chatGroup.missing?.length || 0;
        return {
          title: chatGroup.name,
          tag: chatGroup.kind === "preset" ? "内置群" : "群聊",
          sub: missing
            ? `还缺 ${missing} 位员工 · 点「一键补齐」自动生成`
            : `群聊 · ${chatGroup.members.length} 位成员`,
          av: chatGroup.emoji || "群",
        };
      }
      return {
        title: project?.name || "项目群聊", tag: "群聊",
        sub: project ? `项目群 · ${project.progress ?? 0}%${project.task_total ? ` · ${project.task_completed ?? 0}/${project.task_total} 任务` : ""}` : "还没有项目群",
        av: "群",
      };
    }
    if (mode === "meeting") return {
      title: meeting?.title || "会议", tag: meeting ? "会议" : "",
      sub: !meeting ? "从单聊或群聊点「发起会议」" : meet ? `${meetingMeta(meet.status).label} · ${meet.participants.length} 位参与` : "正在读取会议…",
      av: "会",
    };
    return {
      title: session?.goal || "作战", tag: session ? "作战" : "",
      sub: !session ? "在指挥台下达目标后自动立项" : war ? `${sessionMeta(war.status).label} · 负责人 ${war.leader_name || "—"}` : "正在读取作战…",
      av: "战",
    };
  })();

  const skillCount = useMemo(() => skills?.length ?? 0, [skills]);
  const lastMsg = msgs[msgs.length - 1];
  // 过程卡片就是可见回执；只有旧消息没有 process 时才兜底显示转圈
  const showThinking = mode === "single" && busy && !lastMsg?.process
    && (!!status || !lastMsg?.streaming || !lastMsg?.content);

  const canSend = mode === "single"
    ? (!!input.trim() || !!files.length) && !busy
    : mode === "group" ? !!input.trim() && !groupBusy
    : mode === "meeting" ? !!input.trim() && !meetBusy && (meet?.status === "RUNNING" || meet?.status === "PAUSED")
    : !!input.trim() && !warBusy;

  const placeholder = mode === "single"
    ? (agent ? `说一句话，让 ${agent.name} 去做…` : "先选一个员工…")
    : mode === "group" ? `对${chatGroup?.name || "项目群"}说一句话…`
    : mode === "meeting" ? (meet?.status === "RUNNING" ? "在会议里插一句话（会进纪要）…" : "会议开始后才能发言")
    : "给作战室下达指令…";

  return (
    <div className="flex h-full min-w-0 flex-col" style={{ background: "var(--background)" }}>
      {/* 会话头 */}
      <div className="flex items-center gap-2.5 px-4 shrink-0"
        style={{ height: 52, borderBottom: "1px solid var(--border)" }}>
        {leftHidden && (
          <button type="button" onClick={onExpandLeft} title="展开左栏 ⌘B" aria-label="展开左栏"
            className="w-8 h-8 -ml-1 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
            </svg>
          </button>
        )}
        <div className="w-7 h-7 rounded-lg flex items-center justify-center text-[11px] font-semibold shrink-0"
          style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
          {headline.av}
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="text-sm font-semibold truncate flex items-center gap-1.5">
            <span className="truncate">{headline.title}</span>
            {headline.tag && (
              <span className="text-[9px] px-1.5 py-0.5 rounded shrink-0"
                style={{ background: "rgba(255,255,255,0.06)", color: "var(--muted)" }}>{headline.tag}</span>
            )}
          </h1>
          <p className="text-[10px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>{headline.sub}</p>
        </div>
        <div className="ml-auto flex items-center gap-1 shrink-0">
          {(mode === "single" || mode === "group") && !classic && (
            <>
              {mode === "group" && chatGroup?.missing?.length ? (
                <button type="button" onClick={ensureMissing} disabled={ensuring}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg disabled:opacity-50"
                  style={{ color: "#fcd34d", border: "1px solid rgba(251,191,36,0.35)", background: "rgba(251,191,36,0.1)" }}>
                  {ensuring ? "正在生成…" : `一键补齐 ${chatGroup.missing.length} 位`}
                </button>
              ) : null}
              <button type="button" onClick={startMeeting}
                className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
                style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
                发起会议
              </button>
              {mode === "single" ? (
                <button type="button" onClick={pullGroup}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
                  style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
                  拉群
                </button>
              ) : chatGroup ? (
                <button type="button" onClick={() => onEditGroup(chatGroup)}
                  className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
                  style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
                  群成员
                </button>
              ) : null}
            </>
          )}
          {mode === "war" && session && (
            <button type="button" onClick={() => onOpenApp(`/warroom/${session.id}`)}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              作战室全屏
            </button>
          )}
          {mode === "meeting" && (
            <button type="button" onClick={() => onOpenApp("/meetings")}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              会议中心
            </button>
          )}
          {(mode === "single" || mode === "war") && (
            <button type="button" onClick={onOpenTrace}
              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06]"
              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>
              查看轨迹
            </button>
          )}
          <NotificationBell />
          <button type="button" onClick={onCollapseOut} title={outHidden ? "展开右栏 ⌘J" : "收起右栏 ⌘J"}
            aria-label={outHidden ? "展开右栏" : "收起右栏"}
            className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-white/[0.06]"
            style={{ color: "var(--muted)" }}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
              strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" />
            </svg>
          </button>
        </div>
      </div>

      {/* 形态切换 */}
      {!classic && (
      <div className="flex items-center gap-2 px-4 shrink-0"
        style={{ height: 40, borderBottom: "1px solid var(--border)", background: "rgba(255,255,255,0.012)" }}>
        <div className="flex items-center gap-0.5 p-0.5 rounded-lg"
          style={{ background: "rgba(255,255,255,0.035)", border: "1px solid var(--border)" }}>
          {MODES.map((m) => {
            const on = mode === m.key;
            return (
              <button key={m.key} type="button" data-mode={m.key} onClick={() => onMode(m.key)}
                className="h-6 px-2 rounded-md text-[10.5px] flex items-center gap-1 transition-colors"
                style={on
                  ? { background: "rgba(99,102,241,0.24)", color: "#e6e7ff" }
                  : { color: "var(--muted)" }}>
                <span aria-hidden>{m.icon}</span>{m.label}
              </button>
            );
          })}
        </div>
        <span className="ml-auto text-[10px] hidden sm:block truncate" style={{ color: "#4a4b58" }}>
          {MODE_HINT[mode]}
        </span>
      </div>
      )}

      {mode === "single" && (
        <ChatProcessBar
          process={lastMsg?.role === "agent" ? lastMsg.process : null}
          agentName={agent?.name}
        />
      )}

      {/* 消息流 */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto ws-scroll px-4 py-4"
        onScroll={() => {
          const el = scrollRef.current;
          if (!el) return;
          atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}>
        <div className="mx-auto flex max-w-[720px] flex-col gap-4">
          {mode === "single" && (
            <>
              {loading && <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>正在读取对话…</p>}

              {!loading && !msgs.length && (
                <EmptyIntro
                  glyph={agent?.name.slice(0, 1) || "⚡"}
                  title={agent ? `我在，${agent.name}。要做什么？` : "先选一个员工，或者说一句话"}
                  hint="一句话下达，我会拆解、派给团队，产出直接进右栏。"
                  suggestions={SUGGESTIONS}
                  onPick={(s) => { setInput(s); taRef.current?.focus(); }}
                />
              )}

              {msgs.map((m, i) => (
                m.role === "boss" ? (
                  <div key={i} className="self-end max-w-[86%]">
                    <div className="rounded-2xl rounded-br-sm px-3.5 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap"
                      style={{ background: "#1a1b26", border: "1px solid #262735" }}>
                      {m.content}
                    </div>
                  </div>
                ) : (
                  <div key={i} className="flex gap-2.5">
                    <div className="w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-semibold shrink-0 mt-0.5"
                      style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                      {(agent?.name || "AI").slice(0, 1)}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-medium">{agent?.name || "员工"}</span>
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                          {agent?.role || ""} {m.created_at ? `· ${clock(m.created_at)}` : ""}
                        </span>
                      </div>
                      <div className="text-[13px] leading-relaxed whitespace-pre-wrap" style={{ color: "#c9cad5" }}>
                        {m.content}
                        {m.streaming && <span className="ws-caret" aria-hidden />}
                      </div>
                      {m.file_name && (
                        <p className="mt-1.5 text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-md"
                          style={{ background: "rgba(99,102,241,0.12)", color: "#c7c8ff" }}>
                          📄 {m.file_name} · 已存入交付中心
                        </p>
                      )}
                      {m.process && (
                        <div className="mt-1.5">
                          <ChatProcessCard
                            process={m.process}
                            agentName={agent?.name}
                            live={!!m.streaming}
                          />
                        </div>
                      )}
                      {m.citations?.length ? <CitationList citations={m.citations} /> : null}
                    </div>
                  </div>
                )
              ))}

              {showThinking && (
                <div className="flex items-center gap-2 text-xs" style={{ color: "var(--muted)" }}>
                  <span className="w-3 h-3 rounded-full border-2 animate-spin"
                    style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} aria-hidden />
                  {status || (files.length ? "正在上传附件…" : `${agent?.name || "员工"} 正在思考…`)}
                </div>
              )}
              <div ref={endRef} />
            </>
          )}

          {mode === "group" && (
            <>
              {!project && !chatGroup && <EmptyHint title="还没有群聊" hint="在左栏「群聊」里新建一个群，或者点内置的业务群 / 产品研发群。" action={{ label: "去项目中心", onClick: () => onOpenApp("/projects") }} />}
              {chatGroup?.missing?.length ? (
                <EmptyHint title={`${chatGroup.name} 还缺 ${chatGroup.missing.length} 位员工`}
                  hint={`点一下按预设直接生成：${chatGroup.missing.map((m) => m.name).join("、")}。生成后就是普通员工，可以继续升级、培养。`}
                  action={{ label: ensuring ? "正在生成…" : `一键补齐 ${chatGroup.missing.length} 位员工`, onClick: ensureMissing }} />
              ) : null}
              {(project || chatGroup) && groupLoading && <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>正在读取群聊…</p>}
              {(project || chatGroup) && !groupLoading && !groupMsgs.length && !chatGroup?.missing?.length && (
                <EmptyHint title={`${chatGroup?.name || project?.name} · 群聊`}
                  hint={chatGroup
                    ? `${chatGroup.members.length} 位成员已就位，说一句话他们会依次回应。`
                    : "在群里说一句话，参与项目的员工会依次回应，结论会自动同步到项目知识库。"} />
              )}
              {groupMsgs.map((m, i) => (
                <ChatBubble key={m.id || i} role={m.role}
                  name={m.agent_name || (m.role === "boss" ? "我" : "系统")}
                  text={m.text} time={m.created_at}
                  process={m.process} live={!!m.streaming} />
              ))}
              {groupBusy && (
                <BusyHint startedAt={groupStartedAt} label="群里正在轮流回应" />
              )}
              <div ref={groupEnd} />
            </>
          )}

          {mode === "meeting" && (
            <>
              {!meeting && <EmptyHint title="还没有会议" hint="从单聊或群聊里点「发起会议」，把相关员工拉进来讨论。" />}
              {meeting && meetLoading && !meet && <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>正在读取会议…</p>}
              {meet && (
                <>
                  <div className="rounded-xl px-4 py-3"
                    style={{ border: "1px solid rgba(251,191,36,0.22)", background: "linear-gradient(135deg,rgba(251,191,36,0.10),rgba(239,68,68,0.04))" }}>
                    <div className="flex items-center gap-2.5">
                      <span className="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
                        style={{ background: "rgba(251,191,36,0.15)", border: "1px solid rgba(251,191,36,0.35)", color: "#fcd34d" }}>
                        ◉
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] font-medium truncate">{meet.title}</p>
                        <p className="text-[10px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>
                          {meetingMeta(meet.status).label} · {meet.participants.length} 位参与
                          {meet.topic ? ` · ${meet.topic}` : ""}
                        </p>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {meet.status === "SCHEDULED" && (
                          <button type="button" onClick={() => meetingAction("start")} disabled={meetBusy}
                            className="text-[11px] px-2.5 py-1.5 rounded-lg disabled:opacity-40"
                            style={{ background: "rgba(99,102,241,0.2)", color: "#dcddff", border: "1px solid rgba(99,102,241,0.4)" }}>
                            开始会议
                          </button>
                        )}
                        {meet.status === "RUNNING" && (
                          <>
                            <button type="button" onClick={() => meetingAction("pause")} disabled={meetBusy}
                              className="text-[11px] px-2.5 py-1.5 rounded-lg hover:bg-white/[0.06] disabled:opacity-40"
                              style={{ color: "var(--muted)", border: "1px solid var(--border)" }}>暂停</button>
                            <button type="button" onClick={() => meetingAction("end")} disabled={meetBusy}
                              className="text-[11px] px-2.5 py-1.5 rounded-lg disabled:opacity-40"
                              style={{ background: "rgba(248,113,113,0.12)", color: "#fca5a5", border: "1px solid rgba(248,113,113,0.3)" }}>
                              结束并生成纪要
                            </button>
                          </>
                        )}
                        {meet.status === "PAUSED" && (
                          <button type="button" onClick={() => meetingAction("resume")} disabled={meetBusy}
                            className="text-[11px] px-2.5 py-1.5 rounded-lg disabled:opacity-40"
                            style={{ background: "rgba(99,102,241,0.2)", color: "#dcddff", border: "1px solid rgba(99,102,241,0.4)" }}>
                            继续
                          </button>
                        )}
                        {meet.status === "COMPLETED" && <span className="text-[10px]" style={{ color: "#6ee7b7" }}>已结束</span>}
                      </div>
                    </div>
                    {meet.participants.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2.5">
                        {meet.participants.map((p) => (
                          <span key={p.agent_id} className="text-[10px] px-2 py-0.5 rounded-md"
                            style={{ background: "rgba(255,255,255,0.04)", color: "var(--muted)", border: "1px solid var(--border)" }}>
                            {p.name} · 发言 {p.spoke_count}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {meet.messages.map((m, i) => (
                    <ChatBubble key={i} role={m.speaker_type === "user" ? "boss" : "agent"}
                      name={m.speaker} text={m.content} />
                  ))}

                  {meet.messages.length === 0 && (
                    <p className="text-xs text-center py-4" style={{ color: "var(--muted)" }}>
                      {meet.status === "RUNNING" ? "员工正在准备第一轮发言…" : "会议还没有开始。"}
                    </p>
                  )}

                  {(meet.decisions.length > 0 || meet.action_items.length > 0) && (
                    <div className="rounded-xl px-3.5 py-3" style={{ border: "1px solid var(--border)", background: "rgba(255,255,255,0.02)" }}>
                      {meet.decisions.length > 0 && (
                        <>
                          <p className="text-[10px] tracking-[0.14em] uppercase mb-1.5" style={{ color: "#55555f" }}>结论</p>
                          {meet.decisions.map((d) => (
                            <p key={d.id} className="text-[12px] py-0.5" style={{ color: "#c9cad5" }}>· {d.content}</p>
                          ))}
                        </>
                      )}
                      {meet.action_items.length > 0 && (
                        <>
                          <p className="text-[10px] tracking-[0.14em] uppercase mt-2 mb-1.5" style={{ color: "#55555f" }}>行动项</p>
                          {meet.action_items.map((a) => (
                            <p key={a.id} className="text-[12px] py-0.5" style={{ color: "#c9cad5" }}>
                              · {a.content}{a.assignee ? ` — ${a.assignee}` : ""}
                            </p>
                          ))}
                        </>
                      )}
                    </div>
                  )}
                  {(meet.minutes || meet.summary) && (
                    <div className="rounded-xl px-3.5 py-3 text-[12px] whitespace-pre-wrap"
                      style={{ border: "1px solid var(--border)", background: "rgba(255,255,255,0.02)", color: "#c9cad5" }}>
                      {meet.minutes || meet.summary}
                    </div>
                  )}
                  <div ref={meetEnd} />
                </>
              )}
            </>
          )}

          {mode === "war" && (
            <>
              {!session && <EmptyHint title="还没有作战" hint="在指挥台下达一个目标，系统会立项、分工、盯进度。" action={{ label: "去指挥台", onClick: () => onOpenApp("/command") }} />}
              {session && warLoading && !war && <p className="text-xs text-center py-6" style={{ color: "var(--muted)" }}>正在读取作战现场…</p>}
              {war && (
                <>
                  <div className="rounded-xl px-4 py-3"
                    style={{ border: "1px solid rgba(99,102,241,0.3)", background: "linear-gradient(135deg,rgba(99,102,241,0.12),rgba(168,85,247,0.04))" }}>
                    <div className="flex items-center gap-2.5">
                      <span className="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
                        style={{ background: "rgba(99,102,241,0.16)", border: "1px solid rgba(99,102,241,0.4)", color: "#c7c8ff" }}>
                        ◈
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] font-medium truncate">{war.goal}</p>
                        <p className="text-[10px] mt-0.5" style={{ color: "var(--muted)" }}>
                          {sessionMeta(war.status).label} · {war.tasks.filter((t) => t.status === "done").length}/{war.tasks.length} 完成
                        </p>
                      </div>
                      <button type="button" onClick={() => onOpenApp(`/warroom/${war.id}`)}
                        className="text-[11px] px-2.5 py-1.5 rounded-lg shrink-0"
                        style={{ background: "rgba(99,102,241,0.2)", color: "#dcddff", border: "1px solid rgba(99,102,241,0.4)" }}>
                        作战室
                      </button>
                    </div>
                    {war.tasks.length > 0 && (
                      <div className="mt-3 space-y-1">
                        {war.tasks.map((t) => {
                          const meta = taskMeta(t.status);
                          return (
                            <div key={t.id} className="flex items-center gap-2 text-[11px]" style={{ color: "#c9cad5" }}>
                              <span className="w-1.5 h-1.5 rounded-full shrink-0"
                                style={{ background: meta.tone === "ok" ? "var(--success)" : meta.tone === "run" ? "var(--accent)" : meta.tone === "bad" ? "var(--danger)" : "#4a4b58" }} />
                              <span className="flex-1 truncate">{t.title}</span>
                              <span className="text-[9px] shrink-0" style={{ color: "#55555f" }}>{t.agent_name}</span>
                              <span className="text-[9px] shrink-0" style={{ color: "var(--muted)" }}>{meta.label}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {warChat.map((m, i) => (
                    <ChatBubble key={m.id || i} role={m.role} name={m.agent_name || (m.role === "boss" ? "我" : "系统")}
                      text={m.text} time={m.created_at} />
                  ))}
                  {!warChat.length && (
                    <p className="text-xs text-center py-3" style={{ color: "var(--muted)" }}>还没有群聊记录，给作战室下一句话试试。</p>
                  )}
                  <div ref={warEnd} />
                </>
              )}
            </>
          )}
        </div>
      </div>

      {/* 输入区 */}
      <div className="px-4 pb-3 shrink-0">
        <div className="mx-auto max-w-[720px] rounded-2xl p-2.5"
          style={{ border: "1px solid #2b2c3a", background: "var(--surface)" }}>
          {mode === "single" && files.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-1 pb-1.5">
              {files.map((f, i) => (
                <span key={`${f.name}-${i}`} className="flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-md"
                  style={{ border: "1px solid var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}>
                  <span className="truncate max-w-[160px]">{f.name}</span>
                  <button type="button" aria-label={`移除 ${f.name}`}
                    onClick={() => setFiles((p) => p.filter((_, idx) => idx !== i))}
                    className="hover:text-red-300">×</button>
                </span>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onSubmitKey}
            rows={1}
            placeholder={placeholder}
            className="w-full resize-none bg-transparent outline-none text-[13px] leading-relaxed px-1.5 py-1 max-h-32"
            style={{ color: "var(--foreground)" }}
          />
          <div className="flex items-center gap-1.5 px-1 pt-1.5 border-t" style={{ borderColor: "var(--border)" }}>
            {mode === "single" && (
              <>
                <input ref={fileRef} type="file" multiple className="hidden"
                  accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.xls,.pptx,.ppt,image/*"
                  onChange={(e) => {
                    const list = Array.from(e.target.files || []);
                    if (list.length) setFiles((p) => [...p, ...list]);
                    e.target.value = "";
                  }} />
                <button type="button" onClick={() => fileRef.current?.click()}
                  className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06]"
                  style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                    strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d="M21 11.5l-8.5 8.5a5 5 0 01-7-7L14 4.5a3.3 3.3 0 014.7 4.7l-8.5 8.5a1.7 1.7 0 01-2.4-2.4L15 8" />
                  </svg>
                  附件
                </button>

                <div className="relative" ref={skillsRef}>
                  <button type="button" onClick={openSkills}
                    className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06]"
                    style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
                    ⚡ 技能{skills !== null ? ` · ${skillCount}` : ""}
                  </button>
                  {skillsOpen && (
                    <div className="absolute bottom-9 left-0 w-64 rounded-xl p-2 z-20 shadow-2xl"
                      style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                      <p className="text-[10px] px-1 pb-1.5" style={{ color: "var(--muted)" }}>
                        全公司可用技能（在技能中心绑定到员工）
                      </p>
                      <div className="max-h-56 overflow-y-auto ws-scroll">
                        {skills?.slice(0, 30).map((s) => (
                          <div key={s.id} className="px-1.5 py-1 rounded text-[11px] hover:bg-white/[0.04]">
                            <span className="truncate">{s.name}</span>
                            <span className="text-[9px] ml-1.5" style={{ color: "#55555f" }}>{s.source_label}</span>
                          </div>
                        ))}
                        {skills?.length === 0 && <p className="px-1.5 py-2 text-[11px]" style={{ color: "var(--muted)" }}>还没有技能</p>}
                      </div>
                      <a href="/skill-center" className="block text-[10px] px-1.5 pt-1.5 text-indigo-300">去技能中心 →</a>
                    </div>
                  )}
                </div>

                <div className="relative" ref={pickerRef}>
                  <button type="button" onClick={() => setPickerOpen((v) => !v)}
                    className="flex items-center gap-1 text-[11px] px-2 py-1.5 rounded-lg hover:bg-white/[0.06] max-w-[140px]"
                    style={{ border: "1px solid rgba(99,102,241,0.35)", background: "rgba(99,102,241,0.1)", color: "#c7c8ff" }}>
                    <span className="truncate">{agent ? agent.name : "选员工"}</span>
                    <span aria-hidden className="opacity-60">▾</span>
                  </button>
                  {pickerOpen && (
                    <div className="absolute bottom-9 left-0 w-56 rounded-xl p-1.5 z-20 shadow-2xl"
                      style={{ border: "1px solid var(--border)", background: "var(--surface-2)" }}>
                      {agents.map((a) => (
                        <button key={a.id} type="button"
                          onClick={() => { onPickAgent(a); setPickerOpen(false); }}
                          className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[11px] text-left hover:bg-white/[0.06]"
                          style={{ color: a.id === agent?.id ? "#fff" : "var(--muted)" }}>
                          <span className="w-5 h-5 rounded-md flex items-center justify-center text-[9px] font-semibold"
                            style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
                            {a.name.slice(0, 1)}
                          </span>
                          <span className="flex-1 truncate">{a.name}</span>
                          {a.role && <span className="text-[9px] truncate max-w-[80px]" style={{ color: "#55555f" }}>{a.role}</span>}
                        </button>
                      ))}
                      {!agents.length && <p className="px-2 py-1.5 text-[11px]" style={{ color: "var(--muted)" }}>还没有员工</p>}
                    </div>
                  )}
                </div>
              </>
            )}

            {mode !== "single" && (
              <span className="text-[11px] px-2 py-1.5 rounded-lg flex items-center gap-1 max-w-[160px]"
                style={{ border: "1px solid rgba(99,102,241,0.35)", background: "rgba(99,102,241,0.1)", color: "#c7c8ff" }}>
                <span className="truncate">
                  {mode === "group" ? (chatGroup?.name || project?.name || "群聊") : mode === "meeting" ? "会议" : "作战室"}
                </span>
              </span>
            )}

            <span className="ml-auto text-[10px] hidden sm:block" style={{ color: "#4a4b58" }}>
              {mode === "single"
                ? (busy ? "生成中 · 可随时停止" : "↵ 发送 · ⇧↵ 换行")
                : (groupBusy || meetBusy || warBusy ? "等待回应…" : "↵ 发送 · ⇧↵ 换行")}
            </span>
            {mode === "single" && busy ? (
              <button type="button" onClick={stop}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg font-medium"
                style={{ background: "rgba(248,113,113,0.14)", color: "#fca5a5", border: "1px solid rgba(248,113,113,0.35)" }}>
                <span className="w-2.5 h-2.5 rounded-[2px]" style={{ background: "currentColor" }} aria-hidden />
                停止
              </button>
            ) : (
              <button type="button" onClick={submit} disabled={!canSend}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg font-medium disabled:opacity-40"
                style={{ background: "linear-gradient(135deg,#6366f1,#4f46e5)", color: "#fff" }}>
                发送
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------- 小部件 ---------------- */

function ChatBubble({ role, name, text, time, process, live }: {
  role: string;
  name: string;
  text: string;
  time?: string | null;
  process?: ChatProcess | null;
  live?: boolean;
}) {
  if (role === "boss") {
    return (
      <div className="self-end max-w-[86%]">
        <div className="rounded-2xl rounded-br-sm px-3.5 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap"
          style={{ background: "#1a1b26", border: "1px solid #262735" }}>
          {text}
        </div>
      </div>
    );
  }
  const system = role === "system";
  return (
    <div className="flex gap-2.5">
      <div className="w-6 h-6 rounded-lg flex items-center justify-center text-[10px] font-semibold shrink-0 mt-0.5"
        style={{ background: system ? "rgba(255,255,255,0.06)" : "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
        {system ? "·" : name.slice(0, 1)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-medium">{name}</span>
          {time && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{clock(time)}</span>}
        </div>
        <div className="text-[13px] leading-relaxed whitespace-pre-wrap" style={{ color: "#c9cad5" }}>
          {text}
          {live && <span className="ws-caret" aria-hidden />}
        </div>
        {process && (
          <div className="mt-1.5">
            <ChatProcessCard process={process} agentName={name} live={live} />
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyIntro({ glyph, title, hint, suggestions, onPick }: {
  glyph: string; title: string; hint: string; suggestions: string[]; onPick: (s: string) => void;
}) {
  return (
    <div className="py-8">
      <div className="w-9 h-9 rounded-xl flex items-center justify-center text-sm font-semibold mb-3"
        style={{ background: "linear-gradient(135deg,#6366f1,#22d3ee)", color: "#fff" }}>
        {glyph}
      </div>
      <p className="text-sm font-medium">{title}</p>
      <p className="text-xs mt-1" style={{ color: "var(--muted)" }}>{hint}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button key={s} type="button" onClick={() => onPick(s)}
            className="text-[11px] px-2.5 py-1.5 rounded-lg text-left hover:border-indigo-500/40"
            style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function EmptyHint({ title, hint, action }: {
  title: string; hint: string; action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="text-xs mt-1.5 max-w-[380px] mx-auto" style={{ color: "var(--muted)" }}>{hint}</p>
      {action && (
        <button type="button" onClick={action.onClick} className="btn-primary mt-4">{action.label}</button>
      )}
    </div>
  );
}
