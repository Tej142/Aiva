package com.example.db1;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.database.Cursor;
import android.widget.*;
import android.view.View;
import android.database.sqlite.SQLiteDatabase;

public class MainActivity extends AppCompatActivity {
    Button ins,upt,dis,del;
    EditText et1,et2,et3;

    SQLiteDatabase db;
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        et1 = findViewById(R.id.et1);
        et2 = findViewById(R.id.et2);
        et3 = findViewById(R.id.et3);
        TextView tv = findViewById(R.id.tv);
        ins = findViewById(R.id.ins);
        dis = findViewById(R.id.dis);
        del = findViewById(R.id.del);
        upt = findViewById(R.id.upt);
        db = openOrCreateDatabase("stu",MODE_PRIVATE,null);
        db.execSQL("CREATE TABLE IF NOT EXISTS stu(id INTEGER PRIMARY KEY,name TEXT,age INTEGER)");
        ins.setOnClickListener(new View.OnClickListener(){
           public void onClick(View v){
               String id = et1.getText().toString();
               String name = et2.getText().toString();
               String age = et3.getText().toString();
               db.execSQL("INSERT INTO stu VALUES("+id+",'"+name+"',"+age+")");
               et1.setText("");
               et2.setText("");
               et3.setText("");
           }
        });
        dis.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                Cursor c = db.rawQuery("SELECT * FROM stu",null);
                tv.setText("");
                while(c.moveToNext()){
                    tv.append("id:"+ c.getInt(0)+"\nName: "+c.getString(1)+"\nAge: "+c.getInt(2)+"\n\n");

                }
                c.close();
            }
        });
        del.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String id = et1.getText().toString();
                db.execSQL("DELETE FROM stu WHERE id ="+id);
                et1.setText("");
                et2.setText("");
                et3.setText("");
            }
        });
        upt.setOnClickListener(new View.OnClickListener(){
            public void onClick(View v){
                String id = et1.getText().toString();
                String name = et2.getText().toString();
                String age = et3.getText().toString();
                db.execSQL("UPDATE stu SET name='"+name+"',age="+age+" WHERE id="+id);
                et1.setText("");
                et2.setText("");
                et3.setText("");
            }
        });

    }
}