import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

def epub_to_txt(epub_path, txt_path):
    book = epub.read_epub(epub_path)
    
    # 按 spine 顺序提取
    all_text = []
    for spine_id in book.spine:
        # spine_id 可能是 (idref, linear) 元组
        idref = spine_id[0] if isinstance(spine_id, tuple) else spine_id
        item = book.get_item_with_id(idref)
        if item is None:
            continue
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            # 去掉脚本和样式
            for tag in soup(['script', 'style']):
                tag.decompose()
            text = soup.get_text(separator='\n\n')
            all_text.append(text)
    
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(all_text))

epub_to_txt('窦占龙憋宝：七杆八金刚.epub', '窦占龙憋宝：七杆八金刚.txt')