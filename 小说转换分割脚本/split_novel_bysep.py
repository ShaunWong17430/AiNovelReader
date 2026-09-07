# -*- coding: utf-8 -*-
import os
import sys

def split_by_chapters(text, max_len=5000, separator='异体——我的绯色天空最新章节TXT----- '):
    """
    按固定分隔符切分小说文本，并控制每个分片不超过 max_len
    """
    # 按固定分隔符切分
    raw_parts = text.split(separator)
    
    # 如果找不到分隔符，直接按 max_len 硬切
    if len(raw_parts) <= 1:
        return [text[i:i+max_len] for i in range(0, len(text), max_len)]
    
    parts = []
    
    # 处理第一个分隔符之前的内容（前言/简介）
    preamble = raw_parts[0].strip()
    if preamble:
        for i in range(0, len(preamble), max_len):
            parts.append(preamble[i:i+max_len])
    
    # 处理后续每个章节块
    for raw in raw_parts[1:]:
        raw = raw.strip()
        if not raw:
            continue
        
        # ✅ 提取标题（第一个非空行）和正文
        lines = raw.splitlines()
        title = ''
        body_lines = []
        for line in lines:
            stripped = line.strip()
            if not title and stripped:
                title = stripped
            else:
                body_lines.append(line)
        
        if not title:
            title = "未命名章节"
        
        body = '\n'.join(body_lines).strip()
        
        # 计算 body 每次最多取多少字（给 title + 换行留位置）
        reserved = len(title) + 1
        chunk_size = max_len - reserved
        
        if chunk_size <= 0:
            # 极端情况：title 本身就快占满 max_len，直接截断
            parts.append(f"{title}\n{body}"[:max_len])
            continue
        
        if len(body) <= chunk_size:
            parts.append(f"{title}\n{body}")
        else:
            # 按动态 chunk_size 切分 body
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