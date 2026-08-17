const { google } = require('googleapis');
const fs = require('fs');
const path = require('path');

// Set up OAuth2 authentication
const oauth2Client = new google.auth.OAuth2(
  process.env.GDRIVE_CLIENT_ID,
  process.env.GDRIVE_CLIENT_SECRET,
  'https://developers.google.com/oauthplayground'
);

oauth2Client.setCredentials({
  refresh_token: process.env.GDRIVE_REFRESH_TOKEN,
});

const drive = google.drive({ version: 'v3', auth: oauth2Client });
const FOLDER_ID = '1MbCNI0XeURT4z8w62zKwdlYllbRkeocq';

// Files to sync
const FILES_TO_UPLOAD = [
  'main.py',
  'requirements.txt',
  'Devin_The_Dude_Anythang.mp3',
  'Do You Believe In Magic.mp3',
];

const MIME_TYPES = {
  '.py': 'text/x-python',
  '.txt': 'text/plain',
  '.mp3': 'audio/mpeg',
};

async function uploadFile(fileName) {
  const filePath = path.join(__dirname, fileName);
  
  if (!fs.existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    return;
  }
  
  const ext = path.extname(fileName).toLowerCase();
  const mimeType = MIME_TYPES[ext] || 'application/octet-stream';
  
  try {
    const response = await drive.files.create({
      requestBody: {
        name: fileName,
        parents: [FOLDER_ID],
      },
      media: {
        mimeType: mimeType,
        body: fs.createReadStream(filePath),
      },
      fields: 'id, name',
    });
    
    console.log(`Uploaded ${response.data.name} (ID: ${response.data.id})`);
  } catch (error) {
    console.error(`Failed to upload ${fileName}:`, error.message);
  }
}

async function syncAll() {
  for (const file of FILES_TO_UPLOAD) {
    await uploadFile(file);
  }
}

syncAll();