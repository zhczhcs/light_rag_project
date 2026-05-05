import React, { useState, useEffect } from 'react';
import { Form, Input, Button, Card, Tabs, message, Select } from 'antd';
import { UserOutlined, LockOutlined, MailOutlined } from '@ant-design/icons';
import axios from 'axios';

const Login: React.FC<{ onLoginSuccess: () => void }> = ({ onLoginSuccess }) => {
  const [loading, setLoading] = useState(false);
  const [departments, setDepartments] = useState<{ id: number; name: string }[]>([]);

  useEffect(() => {
    axios.get('http://localhost:8000/api/auth/departments')
      .then(res => setDepartments(res.data))
      .catch(() => {});
  }, []);

  const handleLogin = async (values: any) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.append('username', values.username);
      params.append('password', values.password);
      
      const res = await axios.post('http://localhost:8000/api/auth/login', params);
      
      // 保存Token
      localStorage.setItem('access_token', res.data.access_token);
      localStorage.setItem('refresh_token', res.data.refresh_token);
      
      message.success('登录成功');
      onLoginSuccess();
      window.location.href = '/'; // 强制跳转并刷新状态
    } catch (error: any) {
      message.error(error.response?.data?.detail || '登录失败');
    } finally {
      setLoading(false);
    }
  };

  const handleRegister = async (values: any) => {
    setLoading(true);
    try {
      await axios.post('http://localhost:8000/api/auth/register', {
        username: values.username,
        email: values.email,
        password: values.password,
        department_id: values.department_id
      });
      message.success('注册成功，请登录');
    } catch (error: any) {
      message.error(error.response?.data?.detail || '注册失败');
    } finally {
      setLoading(false);
    }
  };

  const loginForm = (
    <Form name="login" onFinish={handleLogin} layout="vertical">
      <Form.Item name="username" rules={[{ required: true, message: '请输入用户名!' }]}>
        <Input prefix={<UserOutlined />} placeholder="用户名" size="large" />
      </Form.Item>
      <Form.Item name="password" rules={[{ required: true, message: '请输入密码!' }]}>
        <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large" />
      </Form.Item>
      <Form.Item>
        <Button type="primary" htmlType="submit" loading={loading} block size="large">
          登录
        </Button>
      </Form.Item>
    </Form>
  );

  const registerForm = (
    <Form name="register" onFinish={handleRegister} layout="vertical">
      <Form.Item name="username" rules={[{ required: true, message: '请输入用户名!' }]}>
        <Input prefix={<UserOutlined />} placeholder="用户名" size="large" />
      </Form.Item>
      <Form.Item name="email" rules={[{ required: true, type: 'email', message: '请输入有效的邮箱!' }]}>
        <Input prefix={<MailOutlined />} placeholder="邮箱" size="large" />
      </Form.Item>
      <Form.Item name="password" rules={[{ required: true, message: '请输入密码!' }]}>
        <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large" />
      </Form.Item>
      <Form.Item name="department_id" rules={[{ required: true, message: '请选择所属部门!' }]}>
        <Select placeholder="请选择所属部门" size="large">
          {departments.map(d => (
            <Select.Option key={d.id} value={d.id}>{d.name}</Select.Option>
          ))}
        </Select>
      </Form.Item>
      <Form.Item>
        <Button type="primary" htmlType="submit" loading={loading} block size="large">
          注册
        </Button>
      </Form.Item>
    </Form>
  );

  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', background: '#f0f2f5' }}>
      <Card style={{ width: 400, boxShadow: '0 4px 12px rgba(0,0,0,0.1)' }}>
        <h2 style={{ textAlign: 'center', marginBottom: 24 }}>LightRAG 知识库系统</h2>
        <Tabs defaultActiveKey="1" centered>
          <Tabs.TabPane tab="登录" key="1">
            {loginForm}
          </Tabs.TabPane>
          <Tabs.TabPane tab="注册" key="2">
            {registerForm}
          </Tabs.TabPane>
        </Tabs>
      </Card>
    </div>
  );
};

export default Login;
