from pathlib import Path

from app.services.card_service import CardService

from kkloader import KoikatuCharaData

if __name__ == '__main__':
    ly1 = "D:\\BaiduNetdiskDownload\\重绘流萤 春日手信\\重绘流萤v2.0.png"
    yl1 = "D:\\BaiduNetdiskDownload\\银狼\\重绘银狼.png"
    path = Path("D:\\BaiduNetdiskDownload\\SP琳奈\\kks sp琳奈 abmx修复.png")
    # CardService.parse_character_card_for_kks(path)

    # kc1 = KoikatuCharaData.load("D:\\BaiduNetdiskDownload\\SP琳奈\\sp琳奈.png")
    kc1 = KoikatuCharaData.load(ly1)
    print('kc1.Parameter.version:%s',kc1["Parameter"]["version"])
    kc1["Parameter"]["version"] = '0.0.6'
    print('kc1.Parameter.version:%s', kc1["Parameter"]["version"])
    print('dddd', kc1.Parameter.version)
    print(kc1.header)
    # kc2 = KoikatuCharaData.load("D:\\BaiduNetdiskDownload\\SP琳奈\\kks sp琳奈 abmx修复.png")
    kc2 = KoikatuCharaData.load(yl1)
    print('kc2.Parameter.version:%s', kc2["Parameter"]["version"])
    print(kc2.header)
    print()