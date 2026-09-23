import { useEffect } from 'react';
import { useDidShow, useDidHide } from '@tarojs/taro';
// 全局样式
import './app.scss';
import { useSettings } from '@/store/settings';

function App(props) {
  // 可以使用所有的 React Hooks
  useEffect(() => {
    // 启动时恢复本地设置（演示开关/API/Token，PLAN P3.4）
    useSettings.getState().load();
  }, []);

  // 对应 onShow
  useDidShow(() => {});

  // 对应 onHide
  useDidHide(() => {});

  return props.children;
}

export default App;
