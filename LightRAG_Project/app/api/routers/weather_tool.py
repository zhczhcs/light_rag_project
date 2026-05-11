import json

def simulate_get_weather(location: str) -> str:
    """模拟查询天气的工具函数"""
    mock_weather_db = {
        "北京": {"温度": "22°C", "天空": "晴转多云", "风向": "微风", "建议": "适合出门"},
        "上海": {"温度": "25°C", "天空": "阵雨", "风向": "东南风3级", "建议": "记得带伞"},
        "广州": {"温度": "28°C", "天空": "雷阵雨", "风向": "南风4级", "建议": "尽量待在室内"},
        "深圳": {"温度": "29°C", "天空": "多云", "风向": "南风3级", "建议": "注意防晒"},
        "杭州": {"温度": "26°C", "天空": "阴", "风向": "东风2级", "建议": "气候适宜"}
    }
    
    for city, data in mock_weather_db.items():
        if city in location:
            data_copy = data.copy()
            data_copy["城市"] = city
            return json.dumps(data_copy, ensure_ascii=False)
            
    return json.dumps({
        "城市": location, 
        "温度": "未知", 
        "天空": "无法连接气象卫星",
        "建议": "请您抬头看看窗外"
    }, ensure_ascii=False)

WEATHER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "当用户询问关于天气的任何问题时，必须调用此函数获取实时天气情况。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "要查询天气的城市或地区名称，如：北京，上海"
                }
            },
            "required": ["location"]
        }
    }
}
