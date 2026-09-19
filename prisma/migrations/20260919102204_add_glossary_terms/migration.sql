-- CreateTable
CREATE TABLE "glossary_terms" (
    "id" SERIAL NOT NULL,
    "source_text" TEXT NOT NULL,
    "source_text_lower" TEXT NOT NULL,
    "target_lang" TEXT NOT NULL,
    "translation" TEXT NOT NULL,
    "source" TEXT NOT NULL DEFAULT 'gemini',
    "model" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "glossary_terms_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "glossary_terms_source_text_lower_target_lang_key" ON "glossary_terms"("source_text_lower", "target_lang");
