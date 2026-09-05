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

const FILES_TO_UPLOAD = [
  { localPath: "main.py", driveName: "main.py.txt" },
  { localPath: "requirements.txt", driveName: "requirements.txt" },
  { localPath: "sen/__init__.py", driveName: "__init__.py.txt", folder: "sen" },
  { localPath: "sen/config.py", driveName: "config.py.txt", folder: "sen" },
  { localPath: "sen/handlers.py", driveName: "handlers.py.txt", folder: "sen" },
  { localPath: "sen/media.py", driveName: "media.py.txt", folder: "sen" },
  { localPath: "sen/memory.py", driveName: "memory.py.txt", folder: "sen" },
  { localPath: "sen/search.py", driveName: "search.py.txt", folder: "sen" },
  { localPath: "sen/storage.py", driveName: "storage.py.txt", folder: "sen" },
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

async function ensureFolder(name, parentId) {
  const res = await drive.files.list({
    q: `name = '${name}' and '${parentId}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false`,
    fields: "files(id, name)",
    spaces: "drive",
  });
  if (res.data.files.length > 0) return res.data.files[0].id;
  const created = await drive.files.create({
    requestBody: { name, parents: [parentId], mimeType: "application/vnd.google-apps.folder" },
    fields: "id, name",
  });
  console.log(`Created folder: ${created.data.name} (ID: ${created.data.id})`);
  return created.data.id;
}

async function uploadFile(localPath, driveName, parentId) {
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
      q: `name = '${driveName}' and '${parentId}' in parents and trashed = false`,
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
          parents: [parentId],
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

async function deleteOldFlatFiles(parentId, names) {
  for (const name of names) {
    try {
      const res = await drive.files.list({
        q: `name = '${name}' and '${parentId}' in parents and trashed = false`,
        fields: "files(id, name)",
        spaces: "drive",
      });
      for (const file of res.data.files || []) {
        await drive.files.delete({ fileId: file.id });
        console.log(`Deleted old flat file: ${name}`);
      }
    } catch (e) {
      // ignore
    }
  }
}

async function syncAll() {
  const senFolderId = await ensureFolder("sen", FOLDER_ID);
  await deleteOldFlatFiles(FOLDER_ID, [
    "sen/__init__.py.txt", "sen/config.py.txt", "sen/handlers.py.txt",
    "sen/media.py.txt", "sen/memory.py.txt", "sen/search.py.txt", "sen/storage.py.txt",
  ]);
  for (const item of FILES_TO_UPLOAD) {
    const parentId = item.folder === "sen" ? senFolderId : FOLDER_ID;
    await uploadFile(item.localPath, item.driveName, parentId);
  }
}

syncAll();
