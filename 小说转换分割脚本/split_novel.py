# -*- coding: utf-8 -*-
import os
import re
import sys

def split_by_chapters(text, max_len=5000):
    # ✅ 非贪婪匹配标题，遇到行尾或空白就停；加 re.MULTILINE
    chapter_pattern = re.compile(
        r'^\s*(第[零一二三四五六七八九十百千万0-9]+[章回节].{0,30}?)(?=\s*$|\s*\n)',
        re.MULTILINE
    )
    
    matches = list(chapter_pattern.finditer(text))
    
    if not matches:
        return [text[i:i+max_len] for i in range(0, len(text), max_len)]
    
    parts = []
    
    # 处理第一个章节之前的内容（前言/简介）
    first_start = matches[0].start()
    if first_start > 0:
        preamble = text[:first_start].strip()
        if preamble:
            # 前言也可能超长，按 max_len 硬切
            for i in range(0, len(preamble), max_len):
                parts.append(preamble[i:i+max_len])
    
    # 按章节切分
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        chapter_text = text[start:end].strip()
        title = m.group(1).strip()
        
        # 从匹配结束位置到下一个章节之间的正文
        title_end = m.end()
        body = text[title_end:end].strip()
        
        # ✅ 计算 body 每次最多取多少字（给 title + 换行留位置）
        reserved = len(title) + 1  # 1 是换行符
        chunk_size = max_len - reserved
        
        if chunk_size <= 0:
            # 极端情况：title 本身就快占满 max_len，直接原样放
            parts.append(chapter_text[:max_len])
            continue
        
        if len(body) <= chunk_size:
            # 正文不超，直接拼
            parts.append(f"{title}\n{body}")
        else:
            # ✅ 按动态 chunk_size 切分 body
            body_parts = [body[i:i+chunk_size] for i in range(0, len(body), chunk_size)]
            for idx, bp in enumerate(body_parts):
                if idx == 0:
                    parts.append(f"{title}\n{bp}")
                else:
                    parts.append(f"{title}(续{idx})\n{bp}")
    
    return parts

def main():
    if len(sys.argv) < 2:
        print("用法: python split_novel.py <小说文件路径> [输出目录]")
        sys.exit(1)
    
    input_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) >= 3 else "./chunks"
    
    with open(input_path, 'r', encoding='utf-8') as f:
        text = f.read()
    
    chunks = split_by_chapters(text, max_len=5000)
    
    os.makedirs(output_dir, exist_ok=True)
    
    for idx, chunk in enumerate(chunks, 1):
        filename = os.path.join(output_dir, f"part_{idx:03d}.txt")
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(chunk)
        print(f"已生成: {filename} (字数: {len(chunk)})")
    
    print(f"\n分片完成！共生成 {len(chunks)} 个文件，保存在 {output_dir}")

if __name__ == '__main__':
    main()