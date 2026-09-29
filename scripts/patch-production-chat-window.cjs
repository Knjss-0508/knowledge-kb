const fs = require('node:fs');

const inputPath = process.argv[2];
const outputPath = process.argv[3];
if (!inputPath || !outputPath) {
  throw new Error('用法：node scripts/patch-production-chat-window.cjs <输入 HTML> <输出 HTML>');
}

let html = fs.readFileSync(inputPath, 'utf8');

function replaceOnce(source, search, replacement, label) {
  const count = source.split(search).length - 1;
  if (count !== 1) {
    throw new Error(`${label}匹配次数为 ${count}，停止生成`);
  }
  return source.replace(search, replacement);
}

html = replaceOnce(
  html,
  'var workOrderChatWindow = null;',
  [
    'var workOrderChatWindow = null;',
    'var workOrderChatPendingWindow = null;',
    "var workOrderChatTarget = 'knowledge-kb-workorder-chat';"
  ].join('\n'),
  '聊天窗口全局句柄'
);

const oldChatMethods = `    prepareWorkOrderChat: function(workOrderId, event) {
      this.workOrderChatBlockedUrl = '';
      var url = this.workOrderChatUrl(workOrderId);
      if (!url) return;
      var bounds = this.workOrderChatGeometry(event);
      var features = ['popup=yes', 'width=' + bounds.width, 'height=' + bounds.height,
        'left=' + bounds.left, 'top=' + bounds.top, 'resizable=yes', 'scrollbars=yes'].join(',');
      var chatWindow;
      try {
        // 先打开同源空白窗口，再按外框尺寸定位，最后导航到聊天平台。
        // 远程页面加载后会受同源限制，不能靠 resizeTo/moveTo 调整旧窗口。
        chatWindow = window.open('about:blank', '_blank', features);
      } catch (error) {}
      if (!chatWindow) {
        this.workOrderChatBlockedUrl = url;
        return;
      }
      return chatWindow;
    },
    finishWorkOrderChat: function(workOrderId, chatWindow, event) {
      var url = this.workOrderChatUrl(workOrderId);
      if (!url || !chatWindow || chatWindow.closed) {
        this.workOrderChatBlockedUrl = url;
        return;
      }
      var bounds = this.workOrderChatGeometry(event);
      try {
        chatWindow.resizeTo(bounds.width, bounds.height);
        chatWindow.moveTo(bounds.left, bounds.top);
      } catch (positionError) {
        // 定位失败不影响打开；允许用户手动拖动窗口。
      }
      try {
        chatWindow.opener = null;
        chatWindow.location.replace(url);
      } catch (navigationError) {
        chatWindow.close();
        this.workOrderChatBlockedUrl = url;
        return;
      }
      // 用户点击重新对齐时替换本页创建的旧窗口，避免跨工单残留或堆积。
      try { if (workOrderChatWindow && !workOrderChatWindow.closed) workOrderChatWindow.close(); } catch (closeError) {}
      workOrderChatWindow = chatWindow;
      try { chatWindow.focus(); } catch (focusError) {}
    },`;

const newChatMethods = `    closeWorkOrderChatWindow: function(chatWindow) {
      var target = chatWindow || workOrderChatWindow;
      if (!target) return;
      try {
        if (!target.closed) target.close();
      } catch (closeError) {}
      if (workOrderChatWindow === target) workOrderChatWindow = null;
      if (workOrderChatPendingWindow === target) workOrderChatPendingWindow = null;
    },
    prepareWorkOrderChat: function(workOrderId, event) {
      this.workOrderChatBlockedUrl = '';
      var url = this.workOrderChatUrl(workOrderId);
      if (!url) return;
      if (workOrderChatPendingWindow && workOrderChatPendingWindow !== workOrderChatWindow) {
        this.closeWorkOrderChatWindow(workOrderChatPendingWindow);
      }
      var bounds = this.workOrderChatGeometry(event);
      var features = ['popup=yes', 'width=' + bounds.width, 'height=' + bounds.height,
        'left=' + bounds.left, 'top=' + bounds.top, 'resizable=yes', 'scrollbars=yes'].join(',');
      var chatWindow;
      try {
        // 固定窗口名让浏览器优先复用同一个顶层聊天窗口，避免连续切换积累多个窗口。
        chatWindow = window.open('about:blank', workOrderChatTarget, features);
      } catch (error) {}
      if (!chatWindow) {
        this.workOrderChatBlockedUrl = url;
        return;
      }
      if (chatWindow === workOrderChatWindow) {
        workOrderChatPendingWindow = null;
        return chatWindow;
      }
      workOrderChatPendingWindow = chatWindow;
      return chatWindow;
    },
    finishWorkOrderChat: function(workOrderId, chatWindow, event) {
      var url = this.workOrderChatUrl(workOrderId);
      if (!url || !chatWindow || chatWindow.closed) {
        if (chatWindow && workOrderChatPendingWindow === chatWindow) {
          this.closeWorkOrderChatWindow(chatWindow);
        }
        this.workOrderChatBlockedUrl = url;
        return;
      }
      var bounds = this.workOrderChatGeometry(event);
      try {
        chatWindow.resizeTo(bounds.width, bounds.height);
        chatWindow.moveTo(bounds.left, bounds.top);
      } catch (positionError) {
        // 定位失败不影响打开；允许用户手动拖动窗口。
      }
      try {
        chatWindow.opener = null;
        chatWindow.location.replace(url);
      } catch (navigationError) {
        this.closeWorkOrderChatWindow(chatWindow);
        this.workOrderChatBlockedUrl = url;
        return;
      }
      // 切换成功后关闭旧的当前窗口，只保留刚刚导航的聊天窗口。
      if (workOrderChatWindow && workOrderChatWindow !== chatWindow) {
        this.closeWorkOrderChatWindow(workOrderChatWindow);
      }
      workOrderChatWindow = chatWindow;
      if (workOrderChatPendingWindow === chatWindow) workOrderChatPendingWindow = null;
      try { chatWindow.focus(); } catch (focusError) {}
    },`;

html = replaceOnce(html, oldChatMethods, newChatMethods, '聊天窗口方法');
html = replaceOnce(
  html,
  `          try { if (chatWindow && !chatWindow.closed) chatWindow.close(); } catch (error) {}`,
  `          if (chatWindow && chatWindow !== workOrderChatWindow) {
            self.closeWorkOrderChatWindow(chatWindow);
          }`,
  '旧详情异步回调'
);
html = replaceOnce(
  html,
  `          if (!nextChatHandedOff) {
            try { if (nextChatWindow && !nextChatWindow.closed) nextChatWindow.close(); } catch (error) {}
          }`,
  `          if (!nextChatHandedOff && nextChatWindow && nextChatWindow !== workOrderChatWindow) {
            self.closeWorkOrderChatWindow(nextChatWindow);
          }`,
  '保存失败回收窗口'
);

fs.writeFileSync(outputPath, html);
