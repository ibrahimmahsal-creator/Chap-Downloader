import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

def authenticate_drive():
    gauth = GoogleAuth()
    
    # محاولة تحميل بيانات الاعتماد المحفوظة مسبقاً
    gauth.LoadCredentialsFile("mycreds.txt")
    
    if gauth.credentials is None:
        # إذا لم يكن هناك بيانات محفوظة، اطلب المصادقة
        # سيتم فتح نافذة بالمتصفح، لذلك يفضل عمل هذه الخطوة على جهازك الشخصي أولاً
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        # تحديث التوكن إذا انتهت صلاحيته
        gauth.Refresh()
    else:
        # المصادقة ناجحة
        gauth.Authorize()
        
    # حفظ بيانات الاعتماد للمرات القادمة (مهم جداً لعمل البوت على السيرفر)
    gauth.SaveCredentialsFile("mycreds.txt")
    
    return GoogleDrive(gauth)

def upload_folder_to_drive(folder_path: str, folder_name: str):
    drive = authenticate_drive()
    
    # إنشاء مجلد جديد على درايف
    folder_metadata = {
        'title': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = drive.CreateFile(folder_metadata)
    folder.Upload()
    
    # جعل المجلد متاح للجميع برابط
    folder.InsertPermission({
        'type': 'anyone',
        'value': 'anyone',
        'role': 'reader'
    })

    # رفع الصور داخل المجلد
    for filename in sorted(os.listdir(folder_path)):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            file = drive.CreateFile({
                'title': filename,
                'parents': [{'id': folder['id']}]
            })
            file.SetContentFile(file_path)
            file.Upload()
            
    return folder['alternateLink']
