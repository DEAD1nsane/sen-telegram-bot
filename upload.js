const { google } = require('googleapis');
const fs = require('fs');
const path = require('path');

// 1. Authenticate using the Service Account JSON from your GitHub Secret
const auth = new google.auth.GoogleAuth({
  credentials: JSON.parse(process.env.GOOGLE_CREDENTIALS),
  scopes: ['https://www.googleapis.com/auth/drive.file'],
});

const drive = google.drive({ version: 'v3', auth });
const FOLDER_ID = '1MbCNI0XeURT4z8w62zKwdlYllbRkeocq';

// 2. Define the file you want to upload and your target Drive Folder ID
const FILE_PATH = path.join(__dirname, 'your-file.txt'); // Change to your target file
const FOLDER_ID = 'YOUR_GOOGLE_DRIVE_FOLDER_ID'; // Change to your Google Drive folder ID

async function uploadFile() {
  try {
    const fileMetaData = {
      name: path.basename(FILE_PATH),
      parents: [FOLDER_ID],
    };
    
    const media = {
      mimeType: 'text/plain', // Change to match your file's mime type
      body: fs.createReadStream(FILE_PATH),
    };
    
    const response = await drive.files.create({
      resource: fileMetaData,
      media: media,
      fields: 'id',
    });
    
    console.log('File uploaded successfully. File ID:', response.data.id);
  } catch (error) {
    console.error('Error uploading file:', error);
    process.exit(1);
  }
}

uploadFile();