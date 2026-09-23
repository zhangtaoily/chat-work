export default defineAppConfig({
  pages: ['pages/chat/index', 'pages/approval/index', 'pages/inbox/index', 'pages/mine/index'],
  window: {
    backgroundTextStyle: 'light',
    navigationBarBackgroundColor: '#ffffff',
    navigationBarTitleText: 'chat-work 工作台',
    navigationBarTextStyle: 'black',
    backgroundColor: '#f5f6f7'
  },
  tabBar: {
    color: '#86909c',
    selectedColor: '#165dff',
    backgroundColor: '#ffffff',
    borderStyle: 'black',
    list: [
      {
        pagePath: 'pages/chat/index',
        text: '对话',
        iconPath: 'assets/tabbar/chat.svg',
        selectedIconPath: 'assets/tabbar/chat-selected.svg'
      },
      {
        pagePath: 'pages/approval/index',
        text: '审批',
        iconPath: 'assets/tabbar/approval.svg',
        selectedIconPath: 'assets/tabbar/approval-selected.svg'
      },
      {
        pagePath: 'pages/inbox/index',
        text: '信箱',
        iconPath: 'assets/tabbar/inbox.svg',
        selectedIconPath: 'assets/tabbar/inbox-selected.svg'
      },
      {
        pagePath: 'pages/mine/index',
        text: '我的',
        iconPath: 'assets/tabbar/mine.svg',
        selectedIconPath: 'assets/tabbar/mine-selected.svg'
      }
    ]
  }
})
