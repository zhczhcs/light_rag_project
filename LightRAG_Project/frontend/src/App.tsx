import React, { useState, useEffect, useRef } from 'react';
import { Upload, Button, Input, message, Spin, List, Card, Table, Tag, Popconfirm, Select } from 'antd';
import { UploadOutlined, SendOutlined, RobotOutlined, UserOutlined, ReadOutlined, DeleteOutlined, FileTextOutlined, CopyOutlined } from '@ant-design/icons';
import axios from 'axios';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './App.css';

const { TextArea } = Input;

// 定义接口
interface SourceItem {
  id: number;
  content: string;
  content_full: string;
  highlight_terms: string[];
  source_filename: string;
  score: number;
}

interface Message {
  role: 'user' | 'ai';
  content: string;
  sources?: SourceItem[];
  time?: string;
  isThinking?: boolean; // 新增：是否正在思考标记
}

interface DocItem {
  id: number;
  filename: string;
  upload_time: string;
  file_size: string;
  status: string;
}

const App: React.FC = () => {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'ai', content: '你好！我是你的 LightRAG 智能助手。', time: new Date().toLocaleTimeString() }
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

  // 新增：用于自动滚动的 Ref
  const scrollRef = useRef<HTMLDivElement>(null);

  // 初始化加载文档列表
  useEffect(() => {
    fetchDocuments();
  }, []);

  // 新增：监听消息变化，自动滚动到底部
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]); // 监听消息列表和 loading 状态

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
      message.info('上传成功，正在后台建立索引...'); // 提示用户等待
      
      // 2. 轮询检查状态
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

        // 找到当前上传的文档
        const targetDoc = docs.find((d: any) => d.filename === filename);
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

    } catch (error) {
      console.error(error);
      message.error('上传请求失败');
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
      // 3. 使用 fetch 发起流式请求
      const response = await fetch('http://localhost:8000/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ query: userMsg, mode: "hybrid" }),
      });

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
            } else if (part.type === 'error') {
               console.error("Backend Stream Error:", part.data);
            }
          } catch (e) {
            console.warn("Failed to parse JSON chunk", line);
          }
        }
      }

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
      render: (text: string) => <span><FileTextOutlined /> {text}</span>,
    },
    {
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 80,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => {
        let color = 'default';
        let text = status || '未知';
        if (status === 'completed') { color = 'success'; text = '已完成'; }
        else if (status === 'indexing' || status === 'parsing') { color = 'processing'; text = '索引中'; }
        else if (status === 'failed') { color = 'error'; text = '失败'; }
        return <Tag color={color}>{text}</Tag>;
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 60,
      render: (_: any, record: DocItem) => (
        <Popconfirm title="确定删除吗？" onConfirm={() => handleDelete(record.id)}>
           <Button type="text" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className="app-root">
      <div className="app-header">
        <h2 className="app-title">LightRAG 知识库系统</h2>
      </div>

      <div className="app-content">
        {/* 左侧：加宽一点，用来放表格 */}
        <div className="sidebar">
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
              <div className="stat-value stat-value--success">{docCompleted}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">索引中</div>
              <div className="stat-value stat-value--info">{docIndexing}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">失败</div>
              <div className="stat-value stat-value--danger">{docFailed}</div>
            </div>
          </div>

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
          
          <Upload customRequest={handleUpload} showUploadList={false} accept=".pdf,.txt,.md,.docx,.doc">
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
            okButtonProps={{ danger: true }}
          >
            <Button 
              danger 
              icon={<DeleteOutlined />} 
              block 
              className="delete-all-btn"
              disabled={documents.length === 0}
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
                      </div>
                    )}
                    {item.role === 'ai' && item.sources && item.sources.length > 0 && (() => {
                      const referencedSources = item.sources.filter(src => 
                        item.content.includes(`[${src.id}]`)
                      );
                      const displaySources = referencedSources.length > 0 ? referencedSources : item.sources;
                      return (
                        <div className="sources">
                          <div className="sources-title">📚 引用来源：</div>
                          {displaySources.map((src) => {
                            const key = `${item.time || ''}-${src.id}`;
                            const isExpanded = !!expandedSources[key];
                            const displayText = isExpanded ? src.content_full : src.content;
                            return (
                              <div key={src.id} className="sources-item">
                                <div className="sources-item-title">
                                  【来源文档：{src.source_filename}】
                                </div>
                                <div className="sources-item-content">
                                  [{src.id}] {renderHighlightedText(displayText, src.highlight_terms || [])}
                                </div>
                                {src.content_full && src.content_full !== src.content && (
                                  <Button
                                    type="link"
                                    size="small"
                                    onClick={() => setExpandedSources(prev => ({ ...prev, [key]: !isExpanded }))}
                                    className="sources-toggle"
                                  >
                                    {isExpanded ? '收起' : '展开'}
                                  </Button>
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
      </div>
    </div>
  );
};

export default App;