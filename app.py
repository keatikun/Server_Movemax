<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Real-time Messages</title>
<script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
</head>
<body>
<h1>Messages (Real-time)</h1>
<div id="messages">Loading...</div>

<script>
  const socket = io('https://servermovemax-production.up.railway.app');

  // โหลดข้อมูลเก่าครั้งแรก
  async function loadMessages() {
    const res = await fetch('https://servermovemax-production.up.railway.app/messages');
    const messages = await res.json();
    updateMessages(messages);
  }

  // แสดงข้อความทั้งหมดใน div
  function updateMessages(messages) {
    const container = document.getElementById('messages');
    container.innerHTML = '';
    messages.forEach(msg => {
      container.innerHTML += `
        <div>
          <strong>User ID: ${msg.userId || msg.sender || "Unknown"}</strong>
          <pre>${JSON.stringify(msg.chats || msg.message || {}, null, 2)}</pre>
          <hr/>
        </div>
      `;
    });
  }

  // รอรับข้อความอัพเดตใหม่จาก WebSocket
  socket.on('message_update', msg => {
    console.log('New message update:', msg);

    // โหลดข้อมูลทั้งหมดใหม่อีกครั้ง หรือ
    // คุณอาจจะทำแบบเพิ่ม message ทีละตัวก็ได้
    loadMessages();
  });

  socket.on('connect', () => {
    console.log('Connected to WebSocket');
  });

  loadMessages();
</script>
</body>
</html>
