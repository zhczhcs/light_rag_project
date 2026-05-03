import os
import shutil
import fitz  # PyMuPDF
import docx
from fastapi import UploadFile

async def parse_file_content(file: UploadFile) -> str:
    content = ""
    temp_filename = f"temp_{file.filename}"
    with open(temp_filename, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    try:
        # 👉 情况 1: PDF
        if file.filename.endswith(".pdf"):
            doc = fitz.open(temp_filename)
            try:
                for page in doc:
                    content += page.get_text()
            finally:
                doc.close()
        
        # 👉 情况 2: TXT / MD
        elif file.filename.endswith(".txt") or file.filename.endswith(".md"):
            with open(temp_filename, "r", encoding="utf-8") as f:
                content = f.read()

        # 👉 情况 3: DOCX
        elif file.filename.endswith(".docx"):
            try:
                doc = docx.Document(temp_filename)
                # 提取每一段的文字并拼接
                content = "\n".join([para.text for para in doc.paragraphs])
            except Exception as e:
                print(f"❌ 解析 DOCX 失败: {e}")

    finally:
        if os.path.exists(temp_filename):
            try:
                os.remove(temp_filename)
            except:
                pass
    return content

def build_snippet_around_query(full_text: str, query: str, window: int = 200) -> str:
    """围绕查询关键词居中截取摘要"""
    if not full_text:
        return ""
    
    # 清理查询词，剥离常见后缀
    clean_query = query.rstrip("？?！!。，")
    suffixes = ["是什么意思", "是什么", "的意思", "的定义", "的含义", "有哪些", "怎么样", "怎么用", "的区别", "的特点"]
    for suffix in suffixes:
        if clean_query.endswith(suffix):
            clean_query = clean_query[:-len(suffix)].strip()
            break
    
    # 构建候选关键词列表
    query_terms = []
    if clean_query and len(clean_query) >= 2:
        query_terms.append(clean_query)
    # 按空格/标点分词
    import re
    parts = [p.strip() for p in re.split(r'[\s，。、,]+', query) if len(p.strip()) >= 2]
    for p in parts:
        if p not in query_terms:
            query_terms.append(p)
    
    # 在全文中查找关键词
    lower_text = full_text.lower()
    hit_index = -1
    hit_len = 0
    for term in query_terms:
        idx = lower_text.find(term.lower())
        if idx != -1:
            hit_index = idx
            hit_len = len(term)
            break
    
    if hit_index == -1:
        # 没找到关键词，回退到前 N 字符
        return full_text[:window] + ("..." if len(full_text) > window else "")
    
    # 居中截取
    start = max(0, hit_index - window // 2)
    end = min(len(full_text), hit_index + hit_len + window // 2)
    snippet = full_text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(full_text):
        snippet += "..."
    return snippet
