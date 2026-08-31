require("dotenv").config();
const { google } = require("googleapis");
const fs = require("fs");
const path = require("path");

const oauth2Client = new google.auth.OAuth2(
  process.env.GDRIVE_CLIENT_ID,
  process.env.GDRIVE_CLIENT_SECRET,
  "https://developers.google.com/oauthplayground",
);

oauth2Client.setCredentials({
  refresh_token: process.env.GDRIVE_REFRESH_TOKEN,
});

const drive = google.drive({ version: "v3", auth: oauth2Client });
const FOLDER_ID = "1MbCNI0XeURT4z8w62zKwdlYllbRkeocq";

const expandHome = (filepath) =>
  filepath.startsWith("~") ? filepath.replace("~", process.env.HOME) : filepath;

// Updated target list: Removed main.py and requirements.txt
const FILES_TO_UPLOAD = [
  {
    localPath: expandHome(
      "~/storage/shared/Backups/Termux/.termux.properties.txt",
    ),
    driveName: "termux.properties.txt",
  },
  {
    localPath: expandHome("~/storage/shared/Backups/Termux/.zshrc.txt"),
    driveName: "zshrc.txt",
  },
  {
    localPath: expandHome("~/storage/shared/Backups/Termux/.init.lua.txt"),
    driveName: "init.lua.txt",
  },
  {
    localPath: expandHome(
      "~/storage/shared/Backups/Termux/.darkblood.zsh-theme.txt",
    ),
    driveName: "darkblood.zsh-theme.txt",
  },
];

const MIME_TYPES = {
  ".py": "text/plain",
  ".txt": "text/plain",
};

async function uploadFile(localPath, driveName) {
  let filePath = path.isAbsolute(localPath)
    ? localPath
    : path.join(__dirname, localPath);

  if (!fs.existsSync(filePath)) {
    console.error(`File not found: ${filePath}`);
    return;
  }

  const ext = path.extname(driveName).toLowerCase();
  const mimeType = MIME_TYPES[ext] || "text/plain";

  try {
    const listResponse = await drive.files.list({
      q: `name = '${driveName}' and '${FOLDER_ID}' in parents and trashed = false`,
      fields: "files(id, name)",
      spaces: "drive",
    });

    const existingFiles = listResponse.data.files || [];

    if (existingFiles.length > 0) {
      const fileId = existingFiles[0].id;

      const response = await drive.files.update({
        fileId: fileId,
        media: {
          mimeType: mimeType,
          body: fs.createReadStream(filePath),
        },
        fields: "id, name",
      });

      console.log(
        `Updated existing: ${response.data.name} (ID: ${response.data.id})`,
      );
    } else {
      const response = await drive.files.create({
        requestBody: {
          name: driveName,
          parents: [FOLDER_ID],
        },
        media: {
          mimeType: mimeType,
          body: fs.createReadStream(filePath),
        },
        fields: "id, name",
      });

      console.log(
        `Created new: ${response.data.name} (ID: ${response.data.id})`,
      );
    }
  } catch (error) {
    console.error(`Failed to upload ${localPath}:`, error.message);
  }
}

async function syncAll() {
  for (const item of FILES_TO_UPLOAD) {
    await uploadFile(item.localPath, item.driveName);
  }
}

syncAll();
