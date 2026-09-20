-- AlterTable
ALTER TABLE "users" ADD COLUMN     "drive_autosave_enabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "drive_folder_id" TEXT;
