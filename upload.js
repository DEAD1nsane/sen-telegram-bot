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

// Helper to translate '~' into the actual Termux home path
const expandHome = (filepath) => 
  filepath.startsWith('~') ? filepath.replace('~', process.env.HOME) : filepath;

const FILES_TO_UPLOAD = [
  { local: 'main.py', drive: 'main.py.txt' },
  { local: 'requirements.txt', drive: 'requirements.txt' },
  { localPath: expandHome('/storage/emulated/0/Backups/Termux/.termux.properties.txt'), driveName: 'termux.properties.txt', mimeType: 'text/plain' },
  { localPath: expandHome('/storage/emulated/0/Backups/Termux/.zshrc.txt'), driveName: 'zshrc.txt', mimeType: 'text/plain' }
];

const MIME_TYPES = {
  '.py': 'text/plain',
  '.txt': 'text/plain',
};

async function uploadFile(localPath, driveName) {
  const filePath = path.resolve(__dirname, localPath);
  
  if (!fs.existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    return;
  }
  
  const ext = path.extname(driveName).toLowerCase();
  const mimeType = MIME_TYPES[ext] || 'application/octet-stream';
  
  try {
    // 1. Search if a file with the same name already exists in your target Google Drive folder
    const listResponse = await drive.files.list({
      q: `name = '${driveName}' and '${FOLDER_ID}' in parents and trashed = false`,
      fields: 'files(id, name)',
      spaces: 'drive',
    });
    
    const existingFiles = listResponse.data.files || [];
    
    if (existingFiles.length > 0) {
      // 2. If it exists, UPDATE the file contents instead of creating a duplicate
      const fileId = existingFiles[0].id;
      
      const response = await drive.files.update({
        fileId: fileId,
        media: {
          mimeType: mimeType,
          body: fs.createReadStream(filePath),
        },
        fields: 'id, name',
      });
      
      console.log(`Updated existing: ${response.data.name} (ID: ${response.data.id})`);
    } else {
      // 3. If it does not exist, CREATE a new file
      const response = await drive.files.create({
        requestBody: {
          name: driveName,
          parents: [FOLDER_ID],
        },
        media: {
          mimeType: mimeType,
          body: fs.createReadStream(filePath),
        },
        fields: 'id, name',
      });
      
      console.log(`Created new: ${response.data.name} (ID: ${response.data.id})`);
    }
  } catch (error) {
    console.error(`Failed to upload ${localPath}:`, error.message);
  }
}

async function syncAll() {
  for (const item of FILES_TO_UPLOAD) {
    await uploadFile(item.local, item.drive);
  }
}

syncAll();
