import React, { useEffect, useState } from 'react';
import { Card, Table, Switch, Statistic, Row, Col, List, Button, Tag, message } from 'antd';
import { UserOutlined, FileTextOutlined, MessageOutlined, ArrowLeftOutlined, FireOutlined } from '@ant-design/icons';
import axios from 'axios';

interface UserItem {
	id: number;
	username: string;
	email: string;
	role: string;
	is_active: boolean;
	created_at: string;
	document_count: number;
	session_count: number;
	message_count: number;
}

interface PopularQueryItem {
	query: string;
	count: number;
}

interface StatisticsData {
	total_users: number;
	total_documents: number;
	total_qa_pairs: number;
	popular_queries: PopularQueryItem[];
}

const Admin: React.FC<{ onBack?: () => void }> = ({ onBack }) => {
	const [users, setUsers] = useState<UserItem[]>([]);
	const [statistics, setStatistics] = useState<StatisticsData | null>(null);
	const [loadingUsers, setLoadingUsers] = useState(false);
	const [loadingStats, setLoadingStats] = useState(false);

	useEffect(() => {
		fetchUsers();
		fetchStatistics();
	}, []);

	const fetchUsers = async () => {
		setLoadingUsers(true);
		try {
			const res = await axios.get('http://localhost:8000/api/admin/users');
			setUsers(res.data);
		} catch (error: any) {
			message.error(error.response?.data?.detail || '获取用户列表失败');
		} finally {
			setLoadingUsers(false);
		}
	};

	const fetchStatistics = async () => {
		setLoadingStats(true);
		try {
			const res = await axios.get('http://localhost:8000/api/admin/statistics');
			setStatistics(res.data);
		} catch (error: any) {
			message.error(error.response?.data?.detail || '获取统计数据失败');
		} finally {
			setLoadingStats(false);
		}
	};

	const handleToggleActive = async (userId: number) => {
		try {
			await axios.patch(`http://localhost:8000/api/admin/users/${userId}/toggle-active`);
			message.success('状态已更新');
			fetchUsers();
		} catch (error: any) {
			message.error(error.response?.data?.detail || '更新用户状态失败');
		}
	};

	const columns = [
		{ title: '用户名', dataIndex: 'username', key: 'username' },
		{ title: '邮箱', dataIndex: 'email', key: 'email' },
		{
			title: '角色',
			dataIndex: 'role',
			key: 'role',
			render: (role: string, record: UserItem) => (
				<Tag color={record.username === 'zmq' ? 'gold' : 'blue'}>
					{record.username === 'zmq' ? '管理员' : role}
				</Tag>
			)
		},
		{ title: '文档数', dataIndex: 'document_count', key: 'document_count', width: 90 },
		{ title: '会话数', dataIndex: 'session_count', key: 'session_count', width: 90 },
		{ title: '消息数', dataIndex: 'message_count', key: 'message_count', width: 90 },
		{
			title: '状态',
			key: 'is_active',
			width: 100,
			render: (_: any, record: UserItem) => (
				<Switch
					checked={record.is_active}
					disabled={record.username === 'zmq'}
					onChange={() => handleToggleActive(record.id)}
				/>
			)
		}
	];

	return (
		<div style={{
			flex: 1,
			display: 'flex',
			flexDirection: 'column',
			overflow: 'auto',
			padding: '24px 32px',
			background: '#f5f7fb',
			boxSizing: 'border-box',
			width: '100%',
			minHeight: 0,
		}}>
			<div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
				<h2 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>系统管理后台</h2>
				{onBack && (
					<Button icon={<ArrowLeftOutlined />} onClick={onBack}>
						返回系统
					</Button>
				)}
			</div>

			<Row gutter={[24, 0]} style={{ marginBottom: 24 }}>
				<Col span={8}>
					<Card loading={loadingStats} style={{ borderRadius: 10 }}>
						<Statistic title="用户总数" value={statistics?.total_users ?? 0} prefix={<UserOutlined />} />
					</Card>
				</Col>
				<Col span={8}>
					<Card loading={loadingStats} style={{ borderRadius: 10 }}>
						<Statistic title="文档总数" value={statistics?.total_documents ?? 0} prefix={<FileTextOutlined />} />
					</Card>
				</Col>
				<Col span={8}>
					<Card loading={loadingStats} style={{ borderRadius: 10 }}>
						<Statistic title="问答总量" value={statistics?.total_qa_pairs ?? 0} prefix={<MessageOutlined />} />
					</Card>
				</Col>
			</Row>

			<Row gutter={[24, 0]} style={{ flex: 1, minHeight: 0 }}>
				<Col span={16} style={{ display: 'flex', flexDirection: 'column' }}>
					<Card title="用户使用情况" style={{ borderRadius: 10, flex: 1 }}>
						<Table
							dataSource={users}
							columns={columns}
							rowKey="id"
							loading={loadingUsers}
							pagination={{ pageSize: 10 }}
						/>
					</Card>
				</Col>
				<Col span={8} style={{ display: 'flex', flexDirection: 'column' }}>
					<Card title="热门问题 Top 10" loading={loadingStats} style={{ borderRadius: 10, flex: 1 }}>
						<List
							dataSource={statistics?.popular_queries || []}
							locale={{ emptyText: '暂无热门问题' }}
							renderItem={(item, index) => (
								<List.Item style={{ padding: '10px 0' }}>
									<div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', gap: 8 }}>
										<div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, flex: 1, minWidth: 0 }}>
											<Tag color={index < 3 ? 'red' : 'default'} style={{ flexShrink: 0 }}>{index + 1}</Tag>
											<span style={{ color: '#333', wordBreak: 'break-all' }}>{item.query}</span>
										</div>
										<span style={{ color: '#999', flexShrink: 0, whiteSpace: 'nowrap' }}>
											<FireOutlined /> {item.count}
										</span>
									</div>
								</List.Item>
							)}
						/>
					</Card>
				</Col>
			</Row>
		</div>
	);
};

export default Admin;
