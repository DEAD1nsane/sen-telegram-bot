const { google } = require('googleapis');
const fs = require('fs');
const path = require('path');

// 1. Initialize auth using the Service Account JSON
const auth = new google.auth.GoogleAuth({
  keyFile: 'turbo-gemini-5f0464294562.json', 
  scopes: ['https://www.googleapis.com/auth/drive'],
});

const drive = google.drive({ version: 'v3', auth: auth });
const FOLDER_ID = '1MbCNI0XeURT4z8w62zKwdlYllbRkeocq';

// Helper to translate '~' into the actual Termux home path
const expandHome = (filepath) =>
  filepath.startsWith('~') ? filepath.replace('~', process.env.HOME) : filepath;

// FIXED: Standardized the keys to `localPath` and `driveName`
// 1. Specify exact local paths, Drive names, and MIME types here
const FILES_TO_UPLOAD = [
  { localPath: 'main.py', driveName: 'main.py.txt' },
  { localPath: 'requirements.txt', driveName: 'requirements.txt' },
  { localPath: expandHome('~/storage/shared/Backups/Termux/.termux.properties.txt'), driveName: 'termux.properties.txt', mimeType: 'text/plain' },
  { localPath: expandHome('~/storage/shared/Backups/Termux/.zshrc.txt'), driveName: 'zshrc.txt', mimeType: 'text/plain' },
  { localPath: expandHome('~/storage/shared/Backups/Termux/init.lua.txt'), driveName: 'init.lua.txt', mimeType: 'text/plain' },
  { localPath: expandHome('~/storage/shared/Backups/Termux/darkblood.zsh-theme.txt'), driveName: 'darkblood.zsh-theme.txt', mimeType: 'text/plain' }
];

const MIME_TYPES = {
  '.py': 'text/plain',
  '.txt': 'text/plain',
};

async function uploadFile(localPath, driveName) {
  // path.resolve automatically handles absolute paths properly if expandHome returns one
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
    // FIXED: Now accurately targeting the standardized property keys
    await uploadFile(item.localPath, item.driveName);
  }
}

syncAll();