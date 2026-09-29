import React, { useState, useEffect, useRef } from 'react';
import { Upload, Button, Input, message, List, Card, Table, Tag, Popconfirm, Select, Tabs, Switch, ConfigProvider, theme } from 'antd';
import { UploadOutlined, SendOutlined, RobotOutlined, UserOutlined, ReadOutlined, DeleteOutlined, FileTextOutlined, CopyOutlined, LogoutOutlined, MessageOutlined, PlusOutlined, StarOutlined, StarFilled, SettingOutlined, SunOutlined, MoonOutlined, MenuOutlined, MenuFoldOutlined } from '@ant-design/icons';
import axios from 'axios';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './App.css';
import Login from './pages/Login';
import Admin from './pages/Admin';

const { TextArea } = Input;

// Axios 拦截器配置
axios.interceptors.request.use(config => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Token 刷新队列：防止多个 401 请求同时触发多次 refresh（共享单次刷新）
let isRefreshing = false;
let refreshSubscribers: ((token: string) => void)[] = [];

function onTokenRefreshed(newToken: string) {
  refreshSubscribers.forEach(cb => cb(newToken));
  refreshSubscribers = [];
}

/**
 * 解析 JWT payload（不验证签名），用于检查过期时间
 */
function parseJwtPayload(token: string): { exp?: number } | null {
  try {
    const base64 = token.split('.')[1];
    return JSON.parse(atob(base64));
  } catch {
    return null;
  }
}

/**
 * 确保返回一个有效的 access_token（给 fetch 等非 axios 调用使用）
 * - 如果 token 未过期且距离过期 > 60s，直接返回
 * - 如果 token 即将过期或已过期，先刷新再返回新 token
 * - 刷新失败则抛出异常
 */
async function ensureValidToken(): Promise<string> {
  const token = localStorage.getItem('access_token');
  if (!token) throw new Error('未登录');

  const payload = parseJwtPayload(token);
  const now = Math.floor(Date.now() / 1000);

  // 距离过期 > 60s，直接用
  if (payload?.exp && payload.exp - now > 60) {
    return token;
  }

  // token 即将过期或已过期 → 刷新
  // 复用全局 refreshSubscribers 队列，避免和 axios 拦截器冲突
  if (isRefreshing) {
    return new Promise<string>((resolve, reject) => {
      refreshSubscribers.push((newToken: string) => {
        newToken ? resolve(newToken) : reject(new Error('刷新失败'));
      });
    });
  }

  isRefreshing = true;
  try {
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) throw new Error('No refresh token');

    const res = await axios.post('http://localhost:8000/api/auth/refresh', {
      refresh_token: refreshToken
    });

    localStorage.setItem('access_token', res.data.access_token);
    localStorage.setItem('refresh_token', res.data.refresh_token);
    isRefreshing = false;
    onTokenRefreshed(res.data.access_token);
    return res.data.access_token;
  } catch (err) {
    isRefreshing = false;
    refreshSubscribers = [];
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    window.location.reload();
    throw err;
  }
}

axios.interceptors.response.use(
  response => response,
  async error => {
    const originalRequest = error.config;

    // === 500 容错：后端可能正在自愈（SSH 隧道重连），等 2s 重试一次 ===
    if (error.response?.status === 500 && !originalRequest._serverRetry) {
      originalRequest._serverRetry = true;
      console.log('[Axios] 500 错误，等待 2s 后重试（后端可能正在自愈）...');
      await new Promise(r => setTimeout(r, 2000));
      return axios(originalRequest);
    }

    // === 401 Token 刷新（共享单次刷新） ===
    // 跳过登录/注册接口的 401，让 Login 组件自己处理错误提示
    const isAuthEndpoint = originalRequest.url?.includes('/auth/login') || originalRequest.url?.includes('/auth/register');
    if (error.response?.status === 401 && !originalRequest._retry && !isAuthEndpoint) {
      // 如果已经在刷新中，排队等待
      if (isRefreshing) {
        return new Promise(resolve => {
          refreshSubscribers.push((newToken: string) => {
            originalRequest.headers.Authorization = `Bearer ${newToken}`;
            resolve(axios(originalRequest));
          });
        });
      }

      originalRequest._retry = true;
      isRefreshing = true;

      try {
        const refreshToken = localStorage.getItem('refresh_token');
        if (!refreshToken) throw new Error('No refresh token');
        
        const res = await axios.post('http://localhost:8000/api/auth/refresh', {
          refresh_token: refreshToken
        });
        
        localStorage.setItem('access_token', res.data.access_token);
        localStorage.setItem('refresh_token', res.data.refresh_token);
        
        isRefreshing = false;
        onTokenRefreshed(res.data.access_token);
        
        originalRequest.headers.Authorization = `Bearer ${res.data.access_token}`;
        return axios(originalRequest);
      } catch (refreshError) {
        isRefreshing = false;
        refreshSubscribers = [];
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        window.location.reload();
      }
    }
    return Promise.reject(error);
  }
);

// 定义接口
interface SourceItem {
  id: number;
  reference_id: number;
  content: string;
  content_full: string;
  highlight_terms: string[];
  source_filename: string;
  score: number;
}

interface Message {
  id?: number;
  role: 'user' | 'ai';
  content: string;
  sources?: SourceItem[];
  time?: string;
  isThinking?: boolean; // 新增：是否正在思考标记
  is_favorited?: boolean;
  model?: string;   // 新增：使用的模型名
  tokens?: number;  // 新增：本次对话消耗的 Token 数（来自 API usage 字段）
}

interface DocItem {
  id: number;
  filename: string;
  upload_time: string;
  file_size: string;
  status: string;
}

interface SessionItem {
  id: number;
  title: string;
  created_at: string;
  message_count: number;
}

const App: React.FC = () => {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(!!localStorage.getItem('access_token'));
  const [currentUser, setCurrentUser] = useState<any>(null);
  const [viewMode, setViewMode] = useState<'main' | 'admin'>('main');
  const [darkMode, setDarkMode] = useState<boolean>(localStorage.getItem('theme') === 'dark');
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false);

  const handleThemeToggle = (checked: boolean) => {
    setDarkMode(checked);
    localStorage.setItem('theme', checked ? 'dark' : 'light');
  };
  const [messages, setMessages] = useState<Message[]>([
    { role: 'ai', content: '您好！有什么能帮您的吗？我可以帮您查找知识库中的文档、解答问题、分析资料。', time: new Date().toLocaleTimeString() }
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  
  // 新增：文档列表状态
  const [documents, setDocuments] = useState<DocItem[]>([]);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const [filterText, setFilterText] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [expandedSources, setExpandedSources] = useState<Record<string, boolean>>({});
  const [expandedFiles, setExpandedFiles] = useState<Record<string, boolean>>({});

  // 新增：对话历史状态
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null);
  const [favorites, setFavorites] = useState<any[]>([]);

  // 新增：用于自动滚动的 Ref
  const scrollRef = useRef<HTMLDivElement>(null);

  // 初始化加载文档列表
  useEffect(() => {
    if (isAuthenticated) {
      fetchCurrentUser();
      fetchDocuments();
      fetchSessions();
      fetchFavorites();
    }
  }, [isAuthenticated]);

  // 全局定时轮询：只要有 indexing/parsing 文档就每 5 秒自动刷新一次，不受上传超时限制
  useEffect(() => {
    if (!isAuthenticated) return;
    const hasIndexing = documents.some(doc => doc.status === 'indexing' || doc.status === 'parsing');
    if (!hasIndexing) return;
    const timer = setInterval(() => {
      fetchDocuments();
    }, 5000);
    return () => clearInterval(timer);
  }, [documents, isAuthenticated]);

  useEffect(() => {
    if (viewMode === 'admin' && currentUser?.username !== 'zmq') {
      setViewMode('main');
    }
  }, [viewMode, currentUser]);

  const fetchCurrentUser = async () => {
    try {
      const res = await axios.get('http://localhost:8000/api/auth/me');
      setCurrentUser(res.data);
    } catch (error) {
      setCurrentUser(null);
    }
  };

  const fetchFavorites = async () => {
    try {
      const res = await axios.get('http://localhost:8000/api/chat-history/favorites');
      setFavorites(res.data);
    } catch (error) {
      console.error("获取收藏列表失败", error);
    }
  };

  const fetchSessions = async () => {
    try {
      const res = await axios.get('http://localhost:8000/api/chat-history/sessions');
      setSessions(res.data);
      if (res.data.length > 0 && !currentSessionId) {
        handleSwitchSession(res.data[0].id);
      } else if (res.data.length === 0) {
        handleNewChat();
      }
    } catch (error) {
      console.error("获取对话列表失败", error);
    }
  };

  const handleNewChat = async () => {
    try {
      const res = await axios.post('http://localhost:8000/api/chat-history/sessions/new');
      setCurrentSessionId(res.data.session_id);
      setMessages([{ role: 'ai', content: '您好！有什么能帮您的吗？我可以帮您查找知识库中的文档、解答问题、分析资料。', time: new Date().toLocaleTimeString() }]);
      fetchSessions();
    } catch (error) {
      message.error('创建新对话失败');
    }
  };

  const handleSwitchSession = async (sessionId: number) => {
    try {
      const res = await axios.get(`http://localhost:8000/api/chat-history/sessions/${sessionId}/messages`);
      if (res.data.length === 0) {
        setMessages([{ role: 'ai', content: '您好！有什么能帮您的吗？我可以帮您查找知识库中的文档、解答问题、分析资料。', time: new Date().toLocaleTimeString() }]);
      } else {
        setMessages(res.data);
      }
      setCurrentSessionId(sessionId);
    } catch (error) {
      message.error('切换对话失败');
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    setIsAuthenticated(false);
    setCurrentUser(null);
    setViewMode('main');
    setMessages([{ role: 'ai', content: '您好！有什么能帮您的吗？我可以帮您查找知识库中的文档、解答问题、分析资料。', time: new Date().toLocaleTimeString() }]);
    setDocuments([]);
  };

  // 监听消息变化，自动滚动到底部（必须在条件 return 之前，遵守 React Hooks 规则）
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  if (!isAuthenticated) {
    return <Login onLoginSuccess={() => setIsAuthenticated(true)} />;
  }

  const fetchDocuments = async () => {
    setLoadingDocs(true);
    try {
      const res = await axios.get('http://localhost:8000/api/documents');
      setDocuments(res.data);
    } catch (error) {
      console.error("获取列表失败", error);
      message.error("无法连接服务器或数据库，请尝试刷新页面！");
    } finally {
      setLoadingDocs(false);
    }
  };

  const handleUpload = async (info: any) => {
    const formData = new FormData();
    formData.append('file', info.file);
    setUploading(true);
    
    try {
      // 1. 上传文件，触发后台任务
      const res = await axios.post('http://localhost:8000/api/upload', formData);
      const filename = res.data.filename;
      const docId = res.data.doc_id;  // 后端返回的文档 ID，用于精确轮询
      message.info('上传成功，正在后台建立索引...'); // 提示用户等待
      
      // 2. 轮询检查状态（用 doc_id 精确匹配，避免重名文件误判）
      let isFinished = false;
      let attempts = 0;
      const maxAttempts = 60; // 轮询上限，防止死循环 (约2分钟)

      while (!isFinished && attempts < maxAttempts) {
        await new Promise(resolve => setTimeout(resolve, 2000)); // 每 2 秒查一次
        attempts++;

        // 刷新列表获取最新状态
        const docsRes = await axios.get('http://localhost:8000/api/documents');
        const docs = docsRes.data;
        setDocuments(docs); 

        // 用 doc_id 精确查找当前上传的文档（比 filename 更可靠）
        const targetDoc = docId 
          ? docs.find((d: any) => d.id === docId)
          : docs.find((d: any) => d.filename === filename);  // fallback: 旧接口兼容
        if (targetDoc) {
           if (targetDoc.status === 'completed') {
               isFinished = true;
               message.success('索引构建完成！');
               setMessages(prev => [...prev, { role: 'ai', content: `✅ 已学习新文档：${targetDoc.filename}` }]);
           } else if (targetDoc.status === 'failed') {
               isFinished = true;
               message.error('索引构建失败');
           }
           // 如果是 parsing 或 indexing，只需继续轮询即可
        }
      }
      
      if (!isFinished) {
          message.warning('索引仍在处理中，请稍后查看列表状态');
      }

    } catch (error: any) {
      console.error(error);
      // 后端返回 4xx 时，展示具体原因（如格式不支持、空文件）
      const detail = error?.response?.data?.detail;
      message.error(detail || '上传请求失败');
    } finally {
      setUploading(false); // 无论成功失败，最后都停止 loading 动画
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await axios.delete(`http://localhost:8000/api/documents/${id}`);
      message.success('删除成功');
      fetchDocuments(); // 刷新列表
    } catch (error) {
      message.error('删除失败');
    }
  };

  const handleDeleteAll = async () => {
    try {
      await axios.delete('http://localhost:8000/api/documents/all');
      message.success(`已删除所有文档`);
      fetchDocuments(); // 刷新列表
    } catch (error) {
      message.error('删除失败');
    }
  };

  const handleSend = async () => {
    if (!inputText.trim()) return;
    const userMsg = inputText;
    
    let activeSessionId = currentSessionId;
    if (!activeSessionId) {
      try {
        const res = await axios.post('http://localhost:8000/api/chat-history/sessions/new');
        activeSessionId = res.data.session_id;
        setCurrentSessionId(activeSessionId);
        fetchSessions();
      } catch (error) {
        message.error('创建新对话失败');
        return;
      }
    }

    // 1. 先把用户消息显示出来
    setMessages(prev => [...prev, { role: 'user', content: userMsg, time: new Date().toLocaleTimeString() }]);
    setInputText('');
    setLoading(true);

    // 2. 预先添加一条 AI 消息占位，标记为正在思考
    setMessages(prev => [...prev, { 
      role: 'ai', 
      content: '', 
      sources: [],
      isThinking: true,
      time: new Date().toLocaleTimeString()
    }]);

    try {
      // 3. 使用 fetch 发起流式请求（先确保 token 有效，过期则自动刷新）
      let validToken = await ensureValidToken();

      const doFetch = (tk: string) => fetch('http://localhost:8000/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${tk}`
        },
        body: JSON.stringify({ query: userMsg, mode: "mix", session_id: activeSessionId }),
      });

      let response = await doFetch(validToken);

      // 如果仍然 401（极端时序竞争），再刷新一次重试
      if (response.status === 401) {
        console.log('[Chat] fetch 收到 401，尝试刷新 token 后重试...');
        validToken = await ensureValidToken();
        response = await doFetch(validToken);
      }

      if (!response.ok) {
        throw new Error('网络请求失败');
      }

      if (!response.body) return;
      
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || ""; // 保留未完成的行

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const part = JSON.parse(line);
            
            if (part.type === 'sources') {
               // 收到溯源信息 (通常在最后收到)
               const sources = part.data;
               // 从用户问题中提取高亮词（增强版：剥离常见后缀）
               let cleanQuery = userMsg.replace(/[？?！!。，,、]+/g, '').trim();
               const hlSuffixes = ['是什么意思', '是什么', '的意思', '的定义', '的含义', '有哪些', '怎么样', '怎么用', '的区别', '的特点'];
               for (const suffix of hlSuffixes) {
                   if (cleanQuery.endsWith(suffix)) {
                       cleanQuery = cleanQuery.slice(0, -suffix.length).trim();
                       break;
                   }
               }
               const queryTerms: string[] = [];
               // 先把剥离后的核心词加入（如"如履薄冰"）
               if (cleanQuery.length >= 2) {
                   queryTerms.push(cleanQuery);
               }
               // 再按空格分词补充
               const spaceParts = userMsg.replace(/[？?！!。，,、]+/g, ' ').trim().split(/\s+/).filter((t: string) => t.length >= 2);
               for (const sp of spaceParts) {
                   if (!queryTerms.includes(sp)) {
                       queryTerms.push(sp);
                   }
               }
               const enrichedSources = sources.map((src: SourceItem) => ({
                   ...src,
                   highlight_terms: src.highlight_terms && src.highlight_terms.length > 0 ? src.highlight_terms : queryTerms,
               }));
               setMessages(prev => {
                  const newMsgs = [...prev];
                  const lastIdx = newMsgs.length - 1;
                  const lastMsg = newMsgs[lastIdx];
                  if (lastMsg && lastMsg.role === 'ai') {
                    // 使用不可变更新
                    newMsgs[lastIdx] = {
                      ...lastMsg,
                      sources: enrichedSources,
                      isThinking: false
                    };
                  }
                  return newMsgs;
               });
            } else if (part.type === 'content') {
               // 收到流式文本
               const text = part.data;
               setMessages(prev => {
                  const newMsgs = [...prev];
                  const lastIdx = newMsgs.length - 1;
                  const lastMsg = newMsgs[lastIdx];
                  if (lastMsg && lastMsg.role === 'ai') {
                    // ❌ 之前是 last.content += text (可变更新导致 React StrictMode 下重复渲染)
                    // ✅ 现在使用不可变更新
                    newMsgs[lastIdx] = {
                      ...lastMsg,
                      content: lastMsg.content + text,
                      isThinking: false
                    };
                  }
                  return newMsgs;
               });
            } else if (part.type === 'content_correction') {
               // 🔧 引用编号校正：后端过滤引用后重编号，替换 LLM 正文中的旧编号
               const correctedContent = part.data;
               setMessages(prev => {
                  const newMsgs = [...prev];
                  const lastIdx = newMsgs.length - 1;
                  const lastMsg = newMsgs[lastIdx];
                  if (lastMsg && lastMsg.role === 'ai') {
                    newMsgs[lastIdx] = {
                      ...lastMsg,
                      content: correctedContent,
                      isThinking: false
                    };
                  }
                  return newMsgs;
               });
            } else if (part.type === 'message_id') {
               // 收到消息 ID
               const msgId = part.data;
               setMessages(prev => {
                  const newMsgs = [...prev];
                  const lastIdx = newMsgs.length - 1;
                  const lastMsg = newMsgs[lastIdx];
                  if (lastMsg && lastMsg.role === 'ai') {
                    newMsgs[lastIdx] = {
                      ...lastMsg,
                      id: msgId
                    };
                  }
                  return newMsgs;
               });
            } else if (part.type === 'session_title_update') {
               // 🏷️ 第一次对话后自动更新侧边栏会话标题
               const { session_id: sid, title: newTitle } = part.data;
               setSessions(prev => prev.map(s => s.id === sid ? { ...s, title: newTitle } : s));
            } else if (part.type === 'done') {
               // 收到完成元信息（模型名 + 本次消耗 Token 数）
               const { model, tokens } = part.data;
               setMessages(prev => {
                  const newMsgs = [...prev];
                  const lastIdx = newMsgs.length - 1;
                  const lastMsg = newMsgs[lastIdx];
                  if (lastMsg && lastMsg.role === 'ai') {
                    newMsgs[lastIdx] = { ...lastMsg, model, tokens };
                  }
                  return newMsgs;
               });
            } else if (part.type === 'error') {
               console.error("Backend Stream Error:", part.data);
            }
          } catch (e) {
            console.warn("Failed to parse JSON chunk", line);
          }
        }
      }
      
      // 刷新对话列表以更新消息数量
      fetchSessions();

    } catch (error) {
       console.error(error);
       setMessages(prev => {
          const newMsgs = [...prev];
          const lastMsgIndex = newMsgs.length - 1;
          if (newMsgs[lastMsgIndex].role === 'ai') {
            newMsgs[lastMsgIndex].content += '\n\n❌ 请求出错或网络中断';
            newMsgs[lastMsgIndex].isThinking = false;
          }
          return newMsgs;
       });
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      message.success('已复制到剪贴板');
    } catch (error) {
      message.error('复制失败');
    }
  };

  const handleFavorite = async (messageId: number, isFavorited: boolean) => {
    try {
      const token = localStorage.getItem('access_token');
      await axios.post(`http://localhost:8000/api/chat-history/messages/${messageId}/favorite`, {
        is_favorited: isFavorited
      }, {
        headers: { Authorization: `Bearer ${token}` }
      });
      
      setMessages(prev => prev.map(msg => 
        msg.id === messageId ? { ...msg, is_favorited: isFavorited } : msg
      ));
      
      message.success(isFavorited ? '已收藏' : '已取消收藏');
      fetchFavorites();
    } catch (error) {
      message.error('操作失败');
    }
  };

  const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  const renderHighlightedText = (text: string, terms: string[]) => {
    if (!text || !terms || terms.length === 0) return text;
    const safeTerms = terms.filter(t => t && t.length > 0).map(escapeRegExp);
    if (safeTerms.length === 0) return text;
    const regex = new RegExp(`(${safeTerms.join('|')})`, 'gi');
    return text.split(regex).map((part, index) => {
      if (part.match(regex)) {
        return <mark key={`${part}-${index}`} className="highlight-mark">{part}</mark>;
      }
      return <span key={`${part}-${index}`}>{part}</span>;
    });
  };

  // 计算是否正在索引中
  const isIndexing = documents.some(doc => doc.status === 'indexing' || doc.status === 'parsing');

  const docTotal = documents.length;
  const docCompleted = documents.filter(doc => doc.status === 'completed').length;
  // 将 parsing 视为索引中，不再单独显示
  const docIndexing = documents.filter(doc => doc.status === 'indexing' || doc.status === 'parsing').length;
  const docFailed = documents.filter(doc => doc.status === 'failed').length;

  const filteredDocuments = documents.filter(doc => {
    const matchesText = doc.filename.toLowerCase().includes(filterText.toLowerCase().trim());
    const matchesStatus = statusFilter === 'all' ? true : doc.status === statusFilter;
    return matchesText && matchesStatus;
  });

  // 表格列定义
  const columns = [
    {
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
      width: 150,
      ellipsis: true,
      render: (text: string) => (
        <span style={{ wordBreak: 'break-all', whiteSpace: 'normal', fontSize: 12 }}>
          <FileTextOutlined /> {text}
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 65,
      render: (status: string) => {
        let text = status || '未知';
        if (status === 'completed') { text = '已完成'; }
        else if (status === 'indexing' || status === 'parsing') { text = '索引中'; }
        else if (status === 'failed') { text = '失败'; }
        return <span style={{ fontSize: 12, color: '#1a1a1a' }}>{text}</span>;
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 48,
      align: 'right' as const,
      render: (_: any, record: DocItem) => (
        <div style={{ textAlign: 'right' }}>
          <Popconfirm title="确定删除吗？" onConfirm={() => handleDelete(record.id)}>
            <Button type="text" size="small" icon={<DeleteOutlined style={{ color: '#ef4444', fontSize: 14 }} />} style={{ color: '#1a1a1a', padding: '0 4px' }} />
          </Popconfirm>
        </div>
      ),
    },
  ];

  const isAdmin = currentUser?.username === 'zmq';

  return (
    <ConfigProvider theme={{ algorithm: darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
    <div className="app-root" data-theme={darkMode ? 'dark' : 'light'}>
      <div className="app-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          {sidebarCollapsed && (
            <Button
              type="text"
              icon={<MenuOutlined />}
              onClick={() => setSidebarCollapsed(false)}
              style={{ fontSize: 18 }}
            />
          )}
          <h2 className="app-title" style={{ margin: 0 }}>Agentic RAG</h2>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Switch
            checked={darkMode}
            onChange={handleThemeToggle}
            checkedChildren={<MoonOutlined />}
            unCheckedChildren={<SunOutlined />}
          />
          {isAdmin && (
            <Button type="text" icon={<SettingOutlined />} onClick={() => setViewMode('admin')}>
              管理后台
            </Button>
          )}
          <Button type="text" icon={<LogoutOutlined />} onClick={handleLogout}>
            退出登录
          </Button>
        </div>
      </div>

      <div className="app-content" style={viewMode === 'admin' ? { overflow: 'hidden', display: 'flex' } : undefined}>
        {viewMode === 'admin' ? (
          <Admin onBack={() => setViewMode('main')} />
        ) : (
        <>
        {/* 左侧：加宽一点，用来放表格 */}
        <div className={`sidebar ${sidebarCollapsed ? 'sidebar--collapsed' : ''}`}>
          <div className="sidebar-toolbar">
            <Button
              type="text"
              size="small"
              icon={<MenuFoldOutlined />}
              onClick={() => setSidebarCollapsed(true)}
            />
          </div>
          <Tabs defaultActiveKey="1" centered>
            <Tabs.TabPane tab={<span><MessageOutlined /> 对话历史</span>} key="1">
              <div style={{ padding: '0 12px' }}>
                <Button
                  type="dashed"
                  icon={<PlusOutlined />}
                  block
                  onClick={handleNewChat}
                  style={{ marginBottom: 12 }}
                >
                  新建对话
                </Button>
                <div className="session-divider" />
                <List
                  dataSource={sessions}
                  renderItem={item => (
                    <List.Item
                      className={`session-item ${currentSessionId === item.id ? 'active' : ''}`}
                      onClick={() => handleSwitchSession(item.id)}
                    >
                      <List.Item.Meta
                        title={<span className="session-title">{item.title}</span>}
                        description={<span className="session-desc">{item.created_at} · {item.message_count} 条消息</span>}
                      />
                    </List.Item>
                  )}
                />
              </div>
            </Tabs.TabPane>
            <Tabs.TabPane tab={<span><StarOutlined /> 收藏夹</span>} key="2">
              <div style={{ padding: '0 12px' }}>
                <List
                  dataSource={favorites}
                  renderItem={item => (
                    <List.Item
                      className="favorite-item"
                    >
                      <div className="favorite-date">{item.created_at}</div>
                      <div className="favorite-content">
                        {item.content}
                      </div>
                      <div className="favorite-actions">
                        <Button
                          type="text"
                          size="small"
                          danger
                          icon={<StarFilled style={{ color: '#fadb14' }} />}
                          onClick={() => handleFavorite(item.id, false)}
                        >
                          取消收藏
                        </Button>
                      </div>
                    </List.Item>
                  )}
                />
              </div>
            </Tabs.TabPane>
            <Tabs.TabPane tab={<span><ReadOutlined /> 知识库管理</span>} key="3">
              <div style={{ padding: '0 12px' }}>
                <div className="sidebar-title-row">
                  <h3 className="sidebar-title"><ReadOutlined /> 知识库管理</h3>
                  {isIndexing && <span className="indexing-pill">索引中</span>}
                </div>

                <div className="stats-grid">
                  <div className="stat-card">
                    <div className="stat-label">文档总数</div>
                    <div className="stat-value">{docTotal}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">已完成</div>
                    <div className="stat-value">{docCompleted}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">索引中</div>
                    <div className="stat-value">{docIndexing}</div>
                  </div>
                  <div className="stat-card">
                    <div className="stat-label">失败</div>
                    <div className="stat-value">{docFailed}</div>
                  </div>
                </div>

                {currentUser?.department_name && (
                  <div style={{ marginBottom: 12, padding: '4px 12px', background: '#f4f4f4', borderRadius: 6, fontSize: 12, color: '#1a1a1a', display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span>当前部门：<strong>{currentUser.department_name}</strong></span>
                  </div>
                )}

                <div className="filter-row">
                  <Input
                    allowClear
                    placeholder="搜索文件名"
                    value={filterText}
                    onChange={(e) => setFilterText(e.target.value)}
                  />
                  <Select
                    value={statusFilter}
                    onChange={(value) => setStatusFilter(value)}
                    options={[
                      { value: 'all', label: '全部状态' },
                      { value: 'completed', label: '已完成' },
                      { value: 'indexing', label: '索引中' },
                      { value: 'failed', label: '失败' }
                    ]}
                  />
                </div>
                
                <Upload customRequest={handleUpload} showUploadList={false} accept=".pdf,.txt,.md,.docx">
                  <Button type="primary" icon={<UploadOutlined />} loading={uploading} block className="upload-btn">
                    {uploading ? '正在建立索引...' : '上传文档'}
                  </Button>
                </Upload>

                <Popconfirm
                  title="确定要删除所有文档吗？"
                  description="此操作将清空所有文档和向量数据，不可恢复！"
                  onConfirm={handleDeleteAll}
                  okText="确定删除"
                  cancelText="取消"
                >
                  <Button
                    icon={<DeleteOutlined style={{ color: '#ef4444' }} />}
                    block
                    className="delete-all-btn"
                    disabled={documents.length === 0}
                    style={{ borderColor: '#e5e5e5', color: '#1a1a1a' }}
                  >
                    删除所有文档
                  </Button>
                </Popconfirm>

                {/* 文档列表表格 */}
                <div className="table-wrapper">
                  <Table 
                    dataSource={filteredDocuments} 
                    columns={columns} 
                    rowKey="id" 
                    pagination={false} 
                    size="small"
                    loading={loadingDocs}
                    sticky
                    scroll={{ y: 420 }}
                    locale={{ emptyText: '暂无文档' }}
                  />
                </div>
              </div>
            </Tabs.TabPane>
          </Tabs>
        </div>

        {/* 右侧：聊天 */}
        <div className="chat-panel">
          <div className="chat-scroll" ref={scrollRef}>
            <List
              dataSource={messages}
              renderItem={(item) => (
                <div className={`message-row ${item.role === 'user' ? 'message-row--user' : 'message-row--ai'}`}>
                   <div className={`message-meta ${item.role === 'user' ? 'message-meta--user' : 'message-meta--ai'}`}>
                    {item.role === 'ai' ? 
                      <RobotOutlined className="avatar avatar-ai" /> : 
                      <UserOutlined className="avatar avatar-user" />
                    }
                    <span className="role-label">{item.role === 'ai' ? 'AI 助手' : '用户'}</span>
                    {item.time && <span className="time-label">{item.time}</span>}
                  </div>
                  <Card 
                    size="small" 
                    bordered={false}
                    className={`msg-card ${item.role === 'user' ? 'msg-card--user' : 'msg-card--ai'}`}
                  >
                    <div className={item.role === 'user' ? 'user-bubble' : 'markdown-body'}>
                      {item.isThinking ? (
                        <div className="thinking-bubble">
                          <RobotOutlined /> AI 正在思考中<span className="dots-loader"></span>
                        </div>
                      ) : (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
                      )}
                    </div>
                    {item.role === 'ai' && !item.isThinking && (
                      <div className="msg-actions">
                        <Button
                          type="text"
                          size="small"
                          icon={<CopyOutlined />}
                          onClick={() => handleCopy(item.content)}
                        >
                          复制
                        </Button>
                        {item.id && (
                          <Button
                            type="text"
                            size="small"
                            icon={item.is_favorited ? <StarFilled style={{ color: '#fadb14' }} /> : <StarOutlined />}
                            onClick={() => handleFavorite(item.id!, !item.is_favorited)}
                          >
                            {item.is_favorited ? '已收藏' : '收藏'}
                          </Button>
                        )}
                        {item.model && (
                          <span style={{ marginLeft: 'auto', fontSize: '11px', opacity: 0.45, userSelect: 'none' }}>
                            {item.model}{item.tokens ? ` · ${item.tokens} tokens` : ''}
                          </span>
                        )}
                      </div>
                    )}
                    {item.role === 'ai' && item.sources && item.sources.length > 0 && (() => {
                      // 按 reference_id（文件）聚合 chunks
                      const fileGroups = new Map<number, { source_filename: string; chunks: SourceItem[] }>();
                      for (const src of item.sources) {
                        const refId = src.reference_id || src.id || 0;
                        if (!fileGroups.has(refId)) {
                          fileGroups.set(refId, { source_filename: src.source_filename, chunks: [] });
                        }
                        fileGroups.get(refId)!.chunks.push(src);
                      }
                      const groups = Array.from(fileGroups.entries()).map(([refId, group]) => ({
                        refId,
                        ...group,
                      }));
                      // 对每个文件组内的 chunks 按 score 降序排序
                      for (const g of groups) {
                        g.chunks.sort((a, b) => {
                          const sa = a.score ?? -1;
                          const sb = b.score ?? -1;
                          return sb - sa;
                        });
                      }
                      const totalChunks = item.sources.length;
                      return (
                        <div className="sources">
                          <div className="sources-title">引用来源（共{groups.length}个文档，{totalChunks}个片段）：</div>
                          {groups.map((group, fileIdx) => {
                            const fileKey = `${item.time || ''}-file-${group.refId}`;
                            const isFileExpanded = !!expandedFiles[fileKey];
                            return (
                              <div key={fileKey} className="sources-file-group">
                                <div
                                  className="sources-file-header"
                                  onClick={() => setExpandedFiles(prev => ({ ...prev, [fileKey]: !isFileExpanded }))}
                                >
                                  <div className="sources-file-header-left">
                                    <span className="sources-file-name">文件[{fileIdx + 1}]：{group.source_filename}</span>
                                    <span className="sources-file-count">（共{group.chunks.length}个片段）</span>
                                  </div>
                                  <span className={`sources-file-arrow ${isFileExpanded ? 'expanded' : ''}`}>▶</span>
                                </div>
                                {isFileExpanded && (
                                  <div className="sources-file-body">
                                    {group.chunks.map((src, chunkIdx) => {
                                      const chunkKey = `${fileKey}-chunk-${chunkIdx}`;
                                      const isChunkExpanded = !!expandedSources[chunkKey];
                                      const displayText = src.content_full || src.content;
                                      return (
                                        <div key={chunkKey} className="sources-item">
                                          <div
                                            className="sources-item-header"
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              setExpandedSources(prev => ({ ...prev, [chunkKey]: !isChunkExpanded }));
                                            }}
                                          >
                                            <div className="sources-item-header-left">
                                              <span className="sources-item-number">片段{chunkIdx + 1}</span>
                                            </div>
                                            <div className="sources-item-header-right">
                                              {src.score !== undefined && src.score !== null && (
                                                <span className="sources-item-score">相关度: {src.score.toFixed(3)}</span>
                                              )}
                                              <span className={`sources-item-arrow ${isChunkExpanded ? 'expanded' : ''}`}>▶</span>
                                            </div>
                                          </div>
                                          {isChunkExpanded && (
                                            <div className="sources-item-body">
                                              <div className="sources-item-content">
                                                {renderHighlightedText(displayText, src.highlight_terms || [])}
                                              </div>
                                            </div>
                                          )}
                                        </div>
                                      );
                                    })}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      );
                    })()}
                  </Card>
                </div>
              )}
            />
             {isIndexing && <div className="thinking"><ReadOutlined /> 知识库正在学习新文档<span className="dots-loader"></span></div>}
          </div>
          <div className="chat-input">
            <div className="chat-input-inner">
              <TextArea 
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder={isIndexing ? "正在学习新文档中，请稍候..." : "请输入问题..."}
                disabled={isIndexing || loading}
                autoSize={{ minRows: 2, maxRows: 6 }}
                onPressEnter={(e) => !e.shiftKey && (e.preventDefault(), handleSend())}
              />
              <div className="input-help">Enter 发送 · Shift+Enter 换行</div>
              <Button 
                 type="primary" 
                 shape="circle" 
                 size="large" 
                 icon={<SendOutlined />} 
                 onClick={handleSend} 
                 disabled={isIndexing || loading}
                 className="send-btn" 
              />
            </div>
          </div>
        </div>
        </>
        )}
      </div>
    </div>
    </ConfigProvider>
  );
};

export default App;